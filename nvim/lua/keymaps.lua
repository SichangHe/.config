U = require('util')
local M = {}
local current_line = function() return U.api.nvim_win_get_cursor(0)[1] end
local i = function(...) U.key('i', ...) end
local n = function(...) U.key('n', ...) end
local r = function(key) i(key, '<C-g>u' .. key) end
local l = function(key, ...) n('<Space>' .. key, ...) end

function M.set()
    i('jk', '<Esc>')
    i('kj', '<Esc>')
    r('<Space>')
    r('<CR>')
    n('gd', U.buf.definition)
    n('gy', U.buf.type_definition)
    n('gr', U.buf.references)
    n('K', U.buf.hover)
    n('<F2>', U.buf.rename)
    l('w', '<C-W>')
    l('s', U.buf.format)
    l('<S-o>', ':FzfLua oldfiles<CR>')
    l('o', ':FzfLua files<CR>')
    l(';', ':FzfLua commands<CR>')
    l('.', U.buf.code_action)
    l('<Tab>', ':FzfLua buffers<CR>')
    i('<Space><Space>m', '$$<Left>')
    i('<Space><Space>mm', '$$<CR>$$<Esc><S-o>')
    l('f', ':FzfLua blines<CR>')
    l('ff', ':FzfLua grep<CR>')
    l('j', ':FzfLua jumps<CR>')
    l('co', function()
        return ':!co . && co --goto %:' .. current_line() .. '<CR>'
    end, { expr = true })
    l('rrrr', ':lua AllConfig()<CR>:PackerCompile<CR>')
end

return M
