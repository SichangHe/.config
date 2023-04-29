U = require('util')
local M = {}
local n = function(...) U.key('n', ...) end
local v = function(...) U.key('v', ...) end
local i = function(...) U.key('i', ...) end
local s = function(left, right)
    if right == nil then right = left end
    v('<Space>' .. left, 'c' .. left .. '<C-r>*' .. right .. '<Esc>')
end

function M.set()
    n('<Space>n', ':noh<CR>')
    n('<Space>x', ':bd<CR>')
    n('<Tab>', '>>')
    n('<S-Tab>', '<<')
    n('x', [["_x]])
    n('s', [["_s]])
    v('s', [["_s]])
    n('c', [["_c]])
    i('<C-z>', '<C-o>u')
    i('<M-BS>', '<Esc>bce') -- Alt + Backspace delete back one word.
    s('(', ')')
    s('[', ']')
    s('{', '}')
    s('<', '>')
    s('`')
    s("'")
    s('"')
    s([["""]])
    s('$')
    s('$$')
    s('*')
    s('**')
end

return M
