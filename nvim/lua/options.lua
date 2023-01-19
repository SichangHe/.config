U = require('util')
local M = {}
local set = U.set

function M.set()
    set.breakindent = true
    set.confirm = true
    set.endofline = false
    set.expandtab = true
    set.fixendofline = false
    set.list = true
    set.listchars = {
        tab = '- ',
        multispace = '┊   ',
        trail = '·',
    }
    set.mouse = 'a'
    set.number = true
    set.relativenumber = true
    set.scrolloff = 3
    set.shiftwidth = 4
    set.spell = true
    set.splitright = true
    set.tabstop = 4
    set.colorcolumn = "80"
    set.updatetime = 200
    set.guicursor = 'n-v-c-sm:block,i-ci-ve:ver25,r-cr-o:hor20,n-i:blinkon500blinkoff50'
end

return M
