# pylint: disable=E1101
import time
from copy import copy
from functools import reduce
import itertools
import torch
import torch.nn.functional as F
import PIL
PIL.PILLOW_VERSION = PIL.__version__
from torchvision.transforms.functional import to_tensor
import numpy as np
from PIL import Image
from config import config
from progress import updateNode
import logging

def getAnchors(s, ns, l, pad, af, sc):
  n = l - 2 * pad
  step = 1 if l >= af(s) else max(2, int(np.ceil(ns / n)))
  start = np.arange(step, dtype=np.int) * n + pad
  start[0] = 0
  end = start + l
  endSc = end * sc
  if step > 1:
    start[-1] = s - af(s - end[-2] + pad)
    end[-1] = s
    clip = (int(end[-2]) - s) * sc
  else:
    end[-1] = af(s)
    clip = 0
  endSc[-1] = s * sc
  # clip = [0:l, pad:l - pad, ..., end[-2] - s:l]
  return start.tolist(), end.tolist(), clip, step, endSc.tolist()

def prepare(shape, ram, ramCoef, pad, sc, align=8, cropsize=0):
  *_, c, h, w = shape
  n = ram * ramCoef / c
  af = alignF[align]
  s = af(minSize + pad * 2)
  if n < s * s:
    raise MemoryError('Free memory space is {} bytes, which is not enough.'.format(ram))
  ph, pw = max(1, h - pad * 3), max(1, w - pad * 3)
  ns = np.arange(s / align, int(n / (align * s)) + 1, dtype=np.int)
  ms = (n / (align * align) / ns).astype(int)
  ns, ms = ns * align, ms * align
  nn, mn = np.ceil(ph / (ns - 2 * pad)).clip(2), np.ceil(pw / (ms - 2 * pad)).clip(2)
  nn[ns >= h] = 1
  mn[ms >= w] = 1
  ds = nn * mn # minimize number of clips
  ind = np.argwhere(ds == ds.min()).squeeze(1)
  mina = ind[np.abs(ind - len(ds) / 2).argmin()] # pick the size with ratio of width and height closer to 1
  ah, aw, acs = af(h), af(w), af(cropsize)
  ih, iw = (min(acs, ns[mina]), min(acs, ms[mina])) if cropsize > 0 else (ns[mina], ms[mina])
  ih, iw = min(ah, ih), min(aw, iw)
  startH, endH, clipH, stepH, bH = getAnchors(h, ph, ih, pad, af, sc)
  startW, endW, clipW, stepW, wH = getAnchors(w, pw, iw, pad, af, sc)
  padSc, outh, outw = pad * sc, h * sc, w * sc
  if (stepH > 1) and (stepW > 1):
    padImage = identity
    unpad = identity
  elif stepH > 1:
    padImage = padImageReflect((0, aw - w, 0, 0))
    unpad = lambda im: im[:, :, :outw]
  elif stepW > 1:
    padImage = padImageReflect((0, 0, 0, ah - h))
    unpad = lambda im: im[:, :outh]
  else:
    padImage = padImageReflect((0, aw - w, 0, ah - h))
    unpad = lambda im: im[:, :outh, :outw]
  b = ((torch.arange(padSc, dtype=config.dtype(), device=config.device()) / padSc - .5) * 9).sigmoid().view(1, -1)
  def iterClip():
    for i in range(stepH):
      top, bottom, bsc = startH[i], endH[i], bH[i]
      topT = clipH if i == stepH - 1 else (0 if i == 0 else padSc)
      for j in range(stepW):
        left, right, rsc = startW[j], endW[j], wH[j]
        leftT = clipW if j == stepW - 1 else (0 if j == 0 else padSc)
        yield (top, bottom, left, right, topT, leftT, bsc, rsc)
  return iterClip, padImage, unpad, (*shape[:-2], outh, outw), b

def blend(r, x, lt, pad, dim, blend):
  l = r.shape[dim]
  if lt < 0:
    lt = l + lt
  if lt < 1:
    return r, x
  start = lt - pad
  ls, ll = l - start, l - lt
  _, b, c = r.split([start, pad, ll], dim) # share storage
  _, bx, _ = x.split([start, pad, ll], dim)
  b = b * blend + bx * (1 - blend)
  return torch.cat([b, c], dim), x.narrow(dim, start, ls)

def prepareOpt(opt, shape):
  sc, pad = opt.scale, opt.padding
  padSc = pad * sc
  if opt.iterClip is None:
    try:
      freeRam = config.calcFreeMem()
    except:
      raise MemoryError('Can not calculate free memory.')
    if opt.ensemble > 0:
      opt2 = copy(opt)
      opt2.iterClip, opt2.padImage, opt2.unpad, *_ = prepare(transposeShape(shape), freeRam, opt.ramCoef, pad, sc, opt.align, opt.cropsize)
    opt.iterClip, opt.padImage, opt.unpad, outShape, opt.blend = prepare(shape, freeRam, opt.ramCoef, pad, sc, opt.align, opt.cropsize)
    if (not hasattr(opt, 'outShape')) or opt.outShape is None:
      opt.outShape = outShape
    if opt.ensemble > 0:
      opt2.blend = opt.blend
      opt2.outShape = transposeShape(opt.outShape)
      opt.transposedOpt = opt2
  return sc, padSc

def doCrop(opt, x, *args):
  sc, padSc = prepareOpt(opt, x.shape)
  f, bl = opt.modelCached, opt.blend
  x = opt.padImage(opt.unsqueeze(x))
  tmp_image = torch.zeros(opt.outShape, dtype=x.dtype, device=x.device)

  for top, bottom, left, right, topT, leftT, bsc, rsc in opt.iterClip():
    s = x[..., top:bottom, left:right]
    r = opt.squeeze(f(s, *args)[-1])
    t = tmp_image[..., top * sc:bsc, left * sc:rsc]
    q, _ = blend(*blend(opt.unpad(r), t, topT, padSc, -2, bl.t()), leftT, padSc, -1, bl)
    *_, h, w = q.shape
    tmp_image[..., bsc - h:bsc, rsc - w:rsc] = q

  return tmp_image.detach()

def resize(opt, out, pos=0, nodes=[]):
  opt['update'] = True
  if not 'method' in opt:
    opt['method'] = 'bilinear'
  h = w = 1
  def f(im):
    nonlocal h, w
    if opt['update']:
      _, h, w = im.shape
      oriLoad = h * w
      h = round(h * opt['scaleH']) if 'scaleH' in opt else opt['height']
      w = round(w * opt['scaleW']) if 'scaleW' in opt else opt['width']
      newLoad = h * w
      if len(nodes):
        nodes[pos].load = im.nelement()
        newLoad /= oriLoad
        for n in nodes[pos + 1:]:
          n.multipleLoad(newLoad)
          updateNode(n)
      if out['source']:
        opt['update'] = False
    return resizeByTorch(im, w, h, opt['method'])
  return f

def restrictSize(width, height=0, method='bilinear'):
  if not height:
    height = width
  h = w = flag = 0
  def f(im):
    nonlocal h, w, flag
    if not h:
      _, oriHeight, oriWidth = im.shape
      flag = oriHeight <= height and oriWidth <= width
      scaleH = height / oriHeight
      scaleW = width / oriWidth
      if scaleH < scaleW:
        w = round(oriWidth * scaleH)
        h = height
      else:
        h = round(oriHeight * scaleW)
        w = width
    return im if flag else resizeByTorch(im, w, h, method)
  return f

def windowWrap(f, opt, window=2):
  cache = []
  maxBatch = 1 << 7
  h = 0
  getData = lambda: [cache[i:i + window] for i in range(h - window + 1)]
  def init(r=False):
    nonlocal h, cache
    if r and window > 1:
      cache = cache[h - window + 1:h] + [0 for _ in range(maxBatch)]
      h = window - 1
    else:
      cache = [0 for _ in range(window + maxBatch - 1)]
      h = 0
  init()
  def g(inp=None):
    nonlocal h
    b = min(max(1, opt.batchSize), maxBatch)
    if not inp is None:
      cache[h] = inp
      h += 1
      if h >= window + b - 1:
        data = getData()
        init(True)
        return f(data)
    elif h >= window:
      data = getData()
      init()
      return f(data)
  return g

def toNumPy(bitDepth):
  if bitDepth <= 8:
    dtype = np.uint8
  elif bitDepth <= 16:
    dtype = np.uint16
  else:
    dtype = np.int32
  def f(args):
    buffer, height, width = args
    if not buffer:
      return
    image = np.frombuffer(buffer, dtype=dtype)
    return image.reshape((height, width, 3)).astype(np.float32)
  return f

def toBuffer(bitDepth):
  if bitDepth == 8:
    dtype = np.uint8
  elif bitDepth == 16:
    dtype = np.uint16
  return lambda im: im.astype(dtype).tostring() if not im is None else None

def toFloat(image):
  if len(image.shape) == 3:  # to shape (H, W, C)
    image = image.transpose(0, 1).transpose(1, 2)
  else:
    image = image.squeeze(0)
  return image.to(dtype=torch.float)

def toOutput(bitDepth):
  quant = 1 << bitDepth
  if bitDepth <= 8:
    dtype = torch.uint8
  elif bitDepth <= 15:
    dtype = torch.int16
  else:
    dtype = torch.int32
  def f(image):
    image = image.detach() * quant
    image.clamp_(0, quant - 1)
    return image.to(dtype=dtype, device=deviceCPU).numpy()
  return f

def toTorch(bitDepth, dtype, device):
  if bitDepth <= 8:
    return lambda image: to_tensor(image).to(dtype=dtype, device=device)
  quant = 1 << bitDepth
  return lambda image: (to_tensor(image).to(dtype=torch.float, device=device) / quant).to(dtype=dtype)

def writeFile(image, name, context, *args):
  if not name:
    name = genNameByTime()
  elif hasattr(name, 'seek'):
    name.seek(0)
  if image.shape[2] == 1:
    image = image.squeeze(2)
  image = Image.fromarray(image)
  if context.imageMode == 'P':
    image = image.quantize(palette=context.palette)
  image.save(name, *args)
  return name

def readFile(nodes=[], context=None):
  def f(file):
    image = Image.open(file)
    context.imageMode = image.mode
    if image.mode == 'P':
      context.palette = image
      image = image.convert('RGB')
    image = np.array(image)
    for n in nodes:
      n.multipleLoad(image.size)
      updateNode(n)
    if len(nodes):
      p = nodes[0].parent
      updateNode(p)
      p.callback(p)
    if len(image.shape) == 2:
      return image.reshape(*image.shape, 1)
    if image.shape[2] == 3 or image.shape[2] == 4:
      return image
    else:
      raise RuntimeError('Unknown image format')
  return f

def getStateDict(path):
  if not path in weightCache:
    weightCache[path] = torch.load(path, map_location='cpu')
  return weightCache[path]

def initModel(opt, weights=None, key=None, f=lambda opt: opt.modelDef()):
  if key and key in modelCache:
    return modelCache[key].to(dtype=config.dtype(), device=config.device())
  log.info('loading model {}'.format(opt.model))
  model = f(opt)
  if weights:
    log.info('reloading weights')
    if type(weights) == str:
      weights = getStateDict(weights)
    model.load_state_dict(weights)
  for param in model.parameters():
    param.requires_grad_(False)
  model.eval()
  if key:
    modelCache[key] = model
  return model.to(dtype=config.dtype(), device=config.device())

def toInt(o, keys):
  for key in keys:
    if key in o:
      o[key] = int(o[key])

def getPadBy32(img, _):
  *_, oriHeight, oriWidth = img.shape
  width = ceilBy32(oriWidth)
  height = ceilBy32(oriHeight)
  pad = padImageReflect((0, width - oriWidth, 0, height - oriHeight))
  unpad = lambda im: im[:, :oriHeight, :oriWidth]
  return width, height, pad, unpad

def transposeShape(shape):
  tShape = list(shape)
  tShape[-1] = shape[-2]
  tShape[-2] = shape[-1]
  return tShape

class Option():
  def __init__(self, path=''):
    self.ramCoef = 1e-3
    self.padding, self.scale, self.cropsize, self.align = 1, 1, 0, 8
    self.ensemble = 0
    self.model = path
    self.outShape = None
    self.iterClip = None
    self.squeeze = lambda x: x.squeeze(0)
    self.unsqueeze = lambda x: x.unsqueeze(0)

deviceCPU = torch.device('cpu')
outDir = config.outDir
previewFormat = config.videoPreview
previewPath = config.outDir + '/.preview.{}'.format(previewFormat if previewFormat else '')
log = logging.getLogger('Moe')
modelCache = {}
weightCache = {}
genNameByTime = lambda: '{}/output_{}.png'.format(outDir, int(time.time()))
padImageReflect = torch.nn.ReflectionPad2d
identity = lambda x, *_: x
ceilBy = lambda d: lambda x: (-int(x) & -d ^ -1) + 1 # d needed to be a power of 2
ceilBy32 = ceilBy(32)
minSize = 28
alignF = { 1: identity, 8: ceilBy(8), 32: ceilBy(32) }
resizeByTorch = lambda x, width, height, mode='bilinear':\
  F.interpolate(x.unsqueeze(0), size=(height, width), mode=mode, align_corners=False).squeeze()
clean = lambda: torch.cuda.empty_cache()
BGR2RGB = lambda im: np.stack([im[:, :, 2], im[:, :, 1], im[:, :, 0]], axis=2)
BGR2RGBTorch = lambda im: torch.stack([im[2], im[1], im[0]])
toOutput8 = toOutput(8)
apply = lambda v, f: f(v)
transpose = lambda x: x.transpose(-1, -2)
flip = lambda x: x.flip(-1)
flip2 = lambda x: x.flip(-1, -2)
combine = lambda *fs: lambda x: reduce(apply, fs, x)
getTransposedOpt = lambda opt: opt.transposedOpt
trans = [transpose, flip, flip2, combine(flip, transpose), combine(transpose, flip), combine(transpose, flip, transpose), combine(flip2, transpose)]
transInv = [transpose, flip, flip2, trans[4], trans[3], trans[5], trans[6]]
which = [getTransposedOpt, identity, identity, getTransposedOpt, getTransposedOpt, identity, getTransposedOpt]
ensemble = lambda opt: lambda x: reduce((lambda v, t: (v + t[2](doCrop(t[3](opt), t[1](x)))).detach()), zip(range(opt.ensemble), trans, transInv, which), doCrop(opt, x))