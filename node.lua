-- bump 56.0.2924.84-2017-07-20
gl.setup(NATIVE_WIDTH, NATIVE_HEIGHT)

local raw = sys.get_ext "raw_video"
local vid = raw.load_video{
    file = "loading.mp4",
    looped = true,
}

local alpha = 1
local fade = false

util.data_mapper{
    fade = function()
        fade = true
    end
}

function node.render()
    if fade then
        alpha = alpha - 0.01
    end
    if alpha < 0 then
        vid:dispose()
    else
        vid:target(0, 0, WIDTH, HEIGHT):layer(10):alpha(alpha)
        gl.clear(0, 0, 0, 0)
    end
end
