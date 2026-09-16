import numpy as np
from OpenGL.GL import *

from meltygui.core.global_style import BackgroundSettings


class GridDotsBackground:
    def __init__(self, dot_spacing=15.0, dot_size=1.0, emphasis_size=1.2, dot_color=(0.2, 0.23, 0.3, 1.0),
                 bg_color=(0.05, 0.055, 0.07, 1.0)):
        self.dot_spacing = dot_spacing
        self.dot_size = dot_size
        self.emphasis_size = emphasis_size
        self.dot_color = dot_color
        self.bg_color = bg_color
        self.root = None

        vertex_shader = """
        #version 330 core
        layout (location = 0) in vec2 position;
        uniform vec2 resolution;
        void main() {
            gl_Position = vec4(position, 0.0, 1.0);
        }
        """

        fragment_shader = """
        #version 330 core
        out vec4 FragColor;
        uniform vec2 resolution;
        uniform float dotSpacing;
        uniform float dotSize;
        uniform float emphasisSize;
        uniform vec4 dotColor;
        uniform vec4 bgColor;

        void main() {
            vec2 pos = gl_FragCoord.xy;
            vec2 grid = mod(pos, dotSpacing * 5.0);
            vec2 smallGrid = mod(pos, dotSpacing);

            // Check if this is an emphasis point (every 5th dot)
            bool isEmphasis = grid.x < dotSpacing && grid.y < dotSpacing;
            float currentDotSize = isEmphasis ? dotSize * emphasisSize : dotSize;

            float dist = length(smallGrid - dotSpacing/2.0);
            float dot = 1.0 - smoothstep(currentDotSize - 1.0, currentDotSize, dist);
            vec4 bg = vec4(0.05, 0.055, 0.07, 1.0);
            
            FragColor = mix(bgColor, dotColor, dot);
        }
        """
        self.shader = self.create_shader_program(vertex_shader, fragment_shader)

        vertices = np.array([
            -1.0, -1.0,
            1.0, -1.0,
            1.0, 1.0,
            -1.0, 1.0
        ], dtype=np.float32)

        self.vao = glGenVertexArrays(1)
        self.vbo = glGenBuffers(1)

        glBindVertexArray(self.vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferData(GL_ARRAY_BUFFER, vertices.nbytes, vertices, GL_STATIC_DRAW)
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, None)
        glEnableVertexAttribArray(0)

    def set_root(self, root):
        self.root = root

    def create_shader_program(self, vertex_src, fragment_src):
        vertex_shader = glCreateShader(GL_VERTEX_SHADER)
        glShaderSource(vertex_shader, vertex_src)
        glCompileShader(vertex_shader)

        fragment_shader = glCreateShader(GL_FRAGMENT_SHADER)
        glShaderSource(fragment_shader, fragment_src)
        glCompileShader(fragment_shader)

        program = glCreateProgram()
        glAttachShader(program, vertex_shader)
        glAttachShader(program, fragment_shader)
        glLinkProgram(program)

        glDeleteShader(vertex_shader)
        glDeleteShader(fragment_shader)

        return program

    def render(self, width, height):
        if self.root is not None:
            glUseProgram(self.shader)
            glUniform2f(glGetUniformLocation(self.shader, "resolution"), width, height)
            glUniform1f(glGetUniformLocation(self.shader, "dotSpacing"), BackgroundSettings.dot_spacing)
            glUniform1f(glGetUniformLocation(self.shader, "dotSize"), BackgroundSettings.dot_size)
            glUniform1f(glGetUniformLocation(self.shader, "emphasisSize"), BackgroundSettings.emphasis_size)
            glUniform4f(glGetUniformLocation(self.shader, "dotColor"), *BackgroundSettings.dot_color)
            glUniform4f(glGetUniformLocation(self.shader, "bgColor"), *BackgroundSettings.bg_color)

            glBindVertexArray(self.vao)
            glDrawArrays(GL_TRIANGLE_FAN, 0, 4)
        else:

            glUseProgram(self.shader)
            glUniform2f(glGetUniformLocation(self.shader, "resolution"), width, height)
            glUniform1f(glGetUniformLocation(self.shader, "dotSpacing"), self.dot_spacing)
            glUniform1f(glGetUniformLocation(self.shader, "dotSize"), self.dot_size)
            glUniform1f(glGetUniformLocation(self.shader, "emphasisSize"), self.emphasis_size)
            glUniform4f(glGetUniformLocation(self.shader, "dotColor"), *self.dot_color)
            glUniform4f(glGetUniformLocation(self.shader, "bgColor"), *self.bg_color)

            #    vec4 bgColor = vec4(0.05, 0.055, 0.07, 1.0);

            glBindVertexArray(self.vao)
            glDrawArrays(GL_TRIANGLE_FAN, 0, 4)
