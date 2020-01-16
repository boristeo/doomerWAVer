#!doomer/bin/python3

from cgi import parse_qs
import numpy as np
import sys
import os
import pafy
import wave
import av
from unidecode import unidecode
from http import HTTPStatus
import mimetypes
import time


cache = {}

class CacheEntry:
  def __init__(self, streamurl, title, in_file):
    self.time = time.time()
    self.streamurl = streamurl
    self.title = title
    self.file = in_file

def application(env, start_response):
  """Main wsgi entry point"""

  # CONSTANTS
  GET_dir = ''
  GET_chunk_size = 4096

  MAX_VID_LEN_S = 60 * 30

  common_headers = [ 
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Expose-Headers', '*')
  ]
  err_headers = common_headers + [
    ('Content-Type','text/plain')
  ] 
  def data_headers(title):
    return common_headers + [
      ('Content-Type','audio/mpeg'), 
      ('Content-Disposition','attachment; filename=doomer_%s.mp3' % unidecode(title))
    ]
  def static_headers(path):
    filetype = mimetypes.guess_type(path)[0]
    return common_headers + [('Content-Type', filetype)]

  def smsg(status):
    return '%d %s' % (status.value, status.phrase)

  def fail(status, msg=''):
    start_response(smsg(status), err_headers)
    return ('%d %s\r\n%s' % (status.value, status.description, msg)).encode('utf-8')
  # END CONSTANTS


  try:
    method = env['REQUEST_METHOD']  # Should never fail according to PEP

    yturl = None
    if method == 'GET':
      url = env.get('PATH_INFO', '/').lstrip('/')
      if len(url) == 0 or url == 'index.html':
        query = env.get('QUERY_STRING', None)
        if query:
          yturl = parse_qs(query).get('yturl', None)
          if yturl:
            yturl = yturl[0]

    elif method == 'POST':
      # Extracting the necessary POST argument(s) 
      request_body_size = int(env.get('CONTENT_LENGTH', 0))
      args = parse_qs(env['wsgi.input'].read(request_body_size))
      yturl = args.get(b'yturl', None)
      if not yturl:
        yield fail(HTTPStatus.BAD_REQUEST, 'POST request must have a yturl paramter containing the Youtube url')
        return
      yturl = yturl[0].decode('utf-8')

    if yturl:
      print('Requested', yturl)
      cached = cache.get(yturl, None)
      if cached:
        print('Found cache entry from %s' % cached.time)
        streamurl = cached.streamurl
        title = cached.title
        in_file = cached.file or av.open(streamurl, options={'rtsp_transport': 'tcp'})
      else:
        # Request good. Attempt to find video
        video = find_video(yturl)
        if not video:
          yield fail(HTTPStatus.BAD_REQUEST, 'Youtube link was not valid')
          return
        if video.length > MAX_VID_LEN_S:
          yield fail(HTTPStatus.BAD_REQUEST, 'Videos over %d seconds long are not currently supported' % MAX_VID_LEN_S)
          return

        # Video found and valid. Getting audio stream and forwarding to you
        astream = video.getbestaudio(preftype='webm')
        streamurl = astream.url
        title = video.title
        in_file = av.open(streamurl, options={'rtsp_transport': 'tcp'})
        cache[yturl] = CacheEntry(streamurl, title, in_file)

      start_response(smsg(HTTPStatus.OK), data_headers(title))
      for frame in stream_doom(in_file):
        yield frame
      cache[yturl].file = None
      return
      

    elif method == 'GET':
      url = env.get('PATH_INFO', '/').lstrip('/')
      url = url if len(url) > 0 else 'index.html'
      path = os.path.join(GET_dir, url)
      # TODO: Make sure there can't be any / that sneak past forming absolute paths
      try:
        with open(path, 'rb') as f:
          start_response(smsg(HTTPStatus.OK), static_headers(path))
          for chunk in iter(lambda: f.read(GET_chunk_size), b''):
            yield chunk
        return
      except FileNotFoundError:
        yield fail(HTTPStatus.NOT_FOUND)
        return


    else:
      yield fail(HTTPStatus.METHOD_NOT_ALLOWED)
      return
    
  except Exception as e:
    print(e)
    yield fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
    return

  yield fail(HTTPStatus.INTERNAL_SERVER_ERROR, 'Something went incredibly wrong')
  return


def find_video(yturl: str):
  try:
    video = pafy.new(yturl)
    return video
  except:
    return None


def stream_doom(in_file, speed=None, noise=None):
  """ Returns a generator of doomified mp3 frames """

  in_stream = in_file.streams.audio[0]
  in_codec = in_stream.codec_context

  out_codec = av.CodecContext.create('mp3', 'w')
  out_codec.rate = in_codec.rate 
  out_codec.channels = in_codec.channels 
  out_codec.format = in_codec.format 

  resampler = av.AudioResampler(
      format=av.AudioFormat('s16').packed,
      layout=in_codec.layout,
      rate=in_codec.rate * 1.4 if speed is None else 1 / speed,
  )

  if in_codec.channels == 2:
    nf = 'vinyl.wav'
  elif in_codec.channels == 1:
    nf = 'vinylmono.wav'
  else:
    # TODO: Support 5.1 and other configs
    raise Exception('Too many audio channels in stream')

  noise = noise or 0.1
  noise_ratio = int(1 / noise)
  wet = 1 - noise

  def moving_average(a, n=3):
    ret = np.cumsum(a, dtype=float)
    ret[n:] = ret[n:] - ret[:-n]
    return ret[n - 1:] / n

  with wave.open(nf, 'rb') as vinyl:
    vinbuf = vinyl.readframes(int(out_codec.rate * 1.5))
    b = np.floor_divide(np.frombuffer(vinbuf, dtype='i2'), noise_ratio).reshape((1, -1))
    newframe = av.audio.frame.AudioFrame.from_ndarray(b, format='s16', layout=in_codec.layout.name)
    newframe.rate = out_codec.rate
    for p in out_codec.encode(newframe):
      yield p.to_bytes()

    for packet in in_file.demux(in_stream):
      for frame in packet.decode():
        frame.pts = None
        buf = resampler.resample(frame).to_ndarray()[0]
        # reading in a frame of the vinyl
        vinbuf = vinyl.readframes(len(buf) // in_codec.channels)
        if len(vinbuf) < len(buf) * in_codec.channels:
          vinyl.rewind()
          vinbuf = vinyl.readframes(len(buf) // in_codec.channels)
        a = buf * wet
        b = np.frombuffer(vinbuf, dtype='i2') * noise
        mod = moving_average(a + b, n=7).astype('i2').reshape((1, -1))
        
        newframe = av.audio.frame.AudioFrame.from_ndarray(mod, format='s16', layout=in_codec.layout.name)
        newframe.rate = out_codec.rate
        for p in out_codec.encode(newframe):
          yield p.to_bytes()

    for p in out_codec.encode(newframe):
      yield p.to_bytes()

  in_file.close()



def main():
  """A quick way to test the program without bringing up any servers"""
  if len(sys.argv) < 2: sys.exit('Expected a youtube url argument: e.g. ./doomify "http://..."')
  speed = float(sys.argv[2]) if len(sys.argv) >= 3 else None
  video = find_video(sys.argv[1])
  astream = video.getbestaudio(preftype="webm")
  print('Found audio stream', astream)

  with open('output.mp3', 'wb') as out:
    for chunk in stream_doom(astream.url, speed=speed):
      out.write(chunk)
        
if __name__ == '__main__':
  main()
