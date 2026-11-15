// Block compression for Umbra's HD textures: a small C entry point over
// Rich Geldreich's bc7enc_rdo encoders (MIT / public domain):
//   BC1 (DXT1) and BC3 (DXT5) with rgbcx, BC7 with bc7enc.
// bcn.py builds this into a shared library and calls it through ctypes; the
// DDS headers and the mip chain are made in Python.
#include <stdint.h>
#include <string.h>
#include <algorithm>
#include <omp.h>

#include "rgbcx.h"
#include "bc7enc.h"

static bool g_ready = false;

extern "C" int bcn_init(void)
{
	if (!g_ready) {
		rgbcx::init(rgbcx::bc1_approx_mode::cBC1Ideal);
		bc7enc_compress_block_init();
		g_ready = true;
	}
	return 0;
}

// One 4x4 block of RGBA pixels; a block past the image edge repeats the
// last row/column (mips of 2x2 and 1x1).
static inline void fetch(const uint8_t *rgba, int w, int h, int bx, int by, uint8_t px[64])
{
	for (int y = 0; y < 4; y++) {
		const int sy = std::min(by * 4 + y, h - 1);
		for (int x = 0; x < 4; x++) {
			const int sx = std::min(bx * 4 + x, w - 1);
			memcpy(px + (y * 4 + x) * 4, rgba + ((size_t)sy * w + sx) * 4, 4);
		}
	}
}

// fmt: 1 = BC1, 3 = BC3, 7 = BC7. quality: rgbcx level 0..18 for BC1/BC3,
// bc7enc uber level 0..4 for BC7. out must hold ceil(w/4)*ceil(h/4) blocks
// (8 bytes for BC1, 16 otherwise). Returns 0, or -1 for a bad format.
extern "C" int bcn_encode(int fmt, const uint8_t *rgba, int w, int h, int quality, int perceptual,
                          int threads, uint8_t *out)
{
	if (fmt != 1 && fmt != 3 && fmt != 7)
		return -1;
	bcn_init();
	const int bw = (w + 3) / 4, bh = (h + 3) / 4;
	const int bs = fmt == 1 ? 8 : 16;

	bc7enc_compress_block_params p;
	bc7enc_compress_block_params_init(&p);
	if (!perceptual)
		bc7enc_compress_block_params_init_linear_weights(&p);
	p.m_max_partitions = BC7ENC_MAX_PARTITIONS;
	p.m_uber_level = std::min<uint32_t>(BC7ENC_MAX_UBER_LEVEL, (uint32_t)std::max(0, quality));
	const uint32_t level = std::min<uint32_t>(rgbcx::MAX_LEVEL, (uint32_t)std::max(0, quality));

	if (threads > 0)
		omp_set_num_threads(threads);
#pragma omp parallel for schedule(dynamic, 1)
	for (int by = 0; by < bh; by++) {
		uint8_t px[64];
		for (int bx = 0; bx < bw; bx++) {
			fetch(rgba, w, h, bx, by, px);
			uint8_t *dst = out + ((size_t)by * bw + bx) * bs;
			if (fmt == 1)
				// 3-colour blocks allowed, but never the transparent-black texel:
				// these are opaque textures.
				rgbcx::encode_bc1(level, dst, px, true, false);
			else if (fmt == 3)
				rgbcx::encode_bc3_hq(level, dst, px);
			else
				bc7enc_compress_block(dst, px, &p);
		}
	}
	return 0;
}
