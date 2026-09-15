/* Undo PNG scanline filters (RFC 2083 §6). Compiled on first use by image_load.py.
   raw:  h rows of (1 + stride) bytes, filter type byte first.
   out:  h rows of stride bytes.  bpp = bytes per complete pixel. */
#include <stdint.h>
#include <stdlib.h>

static inline int paeth(int a, int b, int c) {
    int p = a + b - c, pa = abs(p - a), pb = abs(p - b), pc = abs(p - c);
    return (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c);
}

int png_unfilter(const uint8_t *raw, uint8_t *out, int h, int stride, int bpp) {
    const uint8_t *prior = NULL;
    for (int y = 0; y < h; y++) {
        const uint8_t *in = raw + (size_t)y * (stride + 1);
        uint8_t *cur = out + (size_t)y * stride;
        int kind = in[0];
        in++;
        switch (kind) {
        case 0:
            for (int i = 0; i < stride; i++) cur[i] = in[i];
            break;
        case 1:
            for (int i = 0; i < stride; i++) cur[i] = in[i] + (i >= bpp ? cur[i - bpp] : 0);
            break;
        case 2:
            for (int i = 0; i < stride; i++) cur[i] = in[i] + (prior ? prior[i] : 0);
            break;
        case 3:
            for (int i = 0; i < stride; i++) {
                int a = i >= bpp ? cur[i - bpp] : 0, b = prior ? prior[i] : 0;
                cur[i] = in[i] + ((a + b) >> 1);
            }
            break;
        case 4:
            for (int i = 0; i < stride; i++) {
                int a = i >= bpp ? cur[i - bpp] : 0;
                int b = prior ? prior[i] : 0;
                int c = (prior && i >= bpp) ? prior[i - bpp] : 0;
                cur[i] = in[i] + paeth(a, b, c);
            }
            break;
        default:
            return -1;
        }
        prior = cur;
    }
    return 0;
}
