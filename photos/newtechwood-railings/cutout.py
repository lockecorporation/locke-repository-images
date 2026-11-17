"""Cut a studio render on pure white out to a transparent PNG (the supplier's own pixels; only the white goes).
Edge pixels are un-mixed from the white: each one's opacity comes from how far it sits between white and the
railing's own color next to it, so dark railings keep no light fringe on a dark page."""
import sys
import numpy as np
from PIL import Image
from scipy import ndimage

def cutout(src, out, pad=12):
    rgb = np.asarray(Image.open(src).convert('RGB')).astype(np.float64)
    mn = rgb.min(axis=2)
    white = mn >= 240
    lab, n = ndimage.label(white)
    sizes = ndimage.sum(white, lab, range(1, n + 1))
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    keep = np.zeros(n + 1, bool)
    for i, s in enumerate(sizes, 1):
        if s >= 150 or i in border: keep[i] = True
    bg = keep[lab]
    band = ndimage.binary_dilation(bg, iterations=3) & ~bg
    fg = ndimage.minimum_filter(mn, size=9)
    alpha = np.ones(mn.shape)
    alpha[bg] = 0
    alpha[band] = np.clip((255 - mn[band]) / np.maximum(255 - fg[band], 10), 0, 1)
    a = alpha[..., None]
    col = np.where(a > 0.01, (rgb - 255 * (1 - a)) / np.maximum(a, 0.01), 0)
    col = np.clip(col, 0, 255)
    rgba = np.dstack([col, alpha * 255]).round().astype(np.uint8)
    im = Image.fromarray(rgba, 'RGBA')
    ys, xs = np.nonzero(alpha > 0.02)
    box = (max(0, xs.min() - pad), max(0, ys.min() - pad), min(im.width, xs.max() + 1 + pad), min(im.height, ys.max() + 1 + pad))
    im = im.crop(box)
    im.save(out, optimize=True)
    return im.size

if __name__ == '__main__':
    print(cutout(sys.argv[1], sys.argv[2]))
