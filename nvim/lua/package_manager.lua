U = require('util')

local lazypath = U.fn.stdpath('data') .. '/lazy/lazy.nvim'
if not U.fs_stat(lazypath) then
    U.fn.system({
        'git',
        'clone',
        '--filter=blob:none',
        'https://github.com/folke/lazy.nvim.git',
        '--branch=stable',
        lazypath,
    })
end
U.rtp:prepend(lazypath)

return require('lazy')
