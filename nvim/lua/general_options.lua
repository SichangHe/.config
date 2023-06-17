U = require('util')
local M = {}
local cmd = U.cmd
local set = U.set

function M.set()
    set.clipboard = 'unnamed'
    set.conceallevel = 2
    set.fixeol = false
    set.mousemodel = 'extend'
    set.timeoutlen = 300
    set.undofile = true
    set.virtualedit = 'block'
    vim.o.background = 'light'
end

return M
