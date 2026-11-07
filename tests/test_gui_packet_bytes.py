"""Packet ingress keeps owned data and validates before mutating residency."""
import struct
import sys

import numpy as np
import pytest
from OpenGL import GL as gl

from conftest import _ensure_gl_context
from test_retained_gui_gpu import pixels
from meltygui.core.rendering._gui_native import TextureCache


@pytest.mark.parametrize('kind', ['bytes', 'bytearray', 'list', 'memoryview'])
def test_packet_input_is_owned_and_sequence_inputs_still_work(kind):
    _ensure_gl_context()
    white = int(gl.glGenTextures(1))
    gl.glBindTexture(gl.GL_TEXTURE_2D, white)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                    bytes([255,255,255,255]))
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    vertices = b''.join(struct.pack('4f4B', x,y,.5,.5,255,0,0,255)
                        for x,y in [(0,0),(8,0),(8,8),(0,8)])
    indices = struct.pack('6I',0,1,2,0,2,3)
    if kind == 'bytearray': vertices,indices=bytearray(vertices),bytearray(indices)
    elif kind == 'list': vertices,indices=list(vertices),list(indices)
    elif kind == 'memoryview': vertices,indices=memoryview(bytearray(vertices)),memoryview(bytearray(indices))
    cache=TextureCache()
    try:
        target=cache.target(1,8,8)
        cache.begin_packet(1)
        refs=sys.getrefcount(vertices),sys.getrefcount(indices)
        cache.add_packet(1,vertices,indices,[(white,6,(0,0,8,8),0,0)])
        assert (sys.getrefcount(vertices),sys.getrefcount(indices)) == refs
        if kind != 'bytes':
            vertices[:]=[0]*len(vertices) if kind=='list' else bytes(len(vertices))
            indices[:]=[0]*len(indices) if kind=='list' else bytes(len(indices))
        del vertices,indices
        cache.render(1)
        np.testing.assert_array_equal(pixels(target,8,8),np.broadcast_to(np.array([255,0,0,255],dtype=np.uint8),(8,8,4)))
        before=cache.stats()
        for v,i,commands in [(b'x',b'',[]),(bytes(80),b'x',[]),
                             (bytes(80),bytes(24),[(white,7,(0,0,8,8),0,0)])]:
            with pytest.raises(RuntimeError):cache.add_packet(1,v,i,commands)
            assert cache.stats()==before
    finally:
        cache.close();gl.glDeleteTextures([white])
