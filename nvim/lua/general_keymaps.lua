U = require('util')
local M = {}
local n = function(...) U.key('n', ...) end
local v = function(...) U.key('v', ...) end
local s = function(left, right)
    if right == nil then right = left end
    v('<Space>' .. left, 'c' .. left .. '<C-r>*' .. right .. '<Esc>')
end

function M.set()
    n('<Space>n', ':noh<CR>')
    n('<Space>x', ':bd<CR>')
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
