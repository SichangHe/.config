U = require('util')
local M = {}
local n = function(...) U.key('n', ...) end
local v = function(...) U.key('v', ...) end
local i = function(...) U.key('i', ...) end
local s = function(left, right)
    if right == nil then right = left end
    v('<Space>' .. left,
        '<Esc>`>a' .. right .. '<Esc>`<i' .. left .. '<Esc>'
    )
end

function M.set()
    n('<Space>n', ':noh<CR>')
    n('<Tab>', '>>')
    n('<S-Tab>', '<<')
    n('x', [["_x]])
    n('c', [["_c]])
    i('<C-z>', '<C-o>u')
    i('<M-BS>', '<Esc>bce')                 -- Alt + Backspace delete back one word.
    i('<C-L>', '<C-G>u<Esc>[s1z=`]a<C-G>u') -- Fix last typo.
    s('(', ')')
    s('[', ']')
    s('{', '}')
    s('<', '>')
    s('`')
    s("'")
    s('"')
    s("|")
    s([["""]])
    s('$')
    s('$$')
    s('*')
    s('**')
    s('~')
    s('~~')
    s([[r#"]], [["#]])

    M.set_macos_option_keys()
end

function M.set_macos_option_keys()
    -- Generate with `generate_option_map.py`. Lua does not Unicode.
    local mapping = {
        { '`', '`', '`' },
        { '1', '¡', '⁄' },
        { '2', '™', '€' },
        { '3', '£', '‹' },
        { '4', '¢', '›' },
        { '5', '∞', 'ﬁ' },
        { '6', '§', 'ﬂ' },
        { '7', '¶', '‡' },
        { '8', '•', '°' },
        { '9', 'ª', '·' },
        { '0', 'º', '‚' },
        { '-', '–', '—' },
        { '=', '≠', '±' },
        { 'q', 'œ', 'Œ' },
        { 'w', '∑', '„' },
        { 'e', '´', '´' },
        { 'r', '®', '‰' },
        { 't', '†', 'ˇ' },
        { 'y', '¥', 'Á' },
        { 'u', '¨', '¨' },
        { 'i', 'ˆ', 'ˆ' },
        { 'o', 'ø', 'Ø' },
        { 'p', 'π', '∏' },
        { '[', '“', '”' },
        { ']', '‘', '’' },
        { [[\]], '«', '»' },
        { 'a', 'å', 'Å' },
        { 's', 'ß', 'Í' },
        { 'd', '∂', 'Î' },
        { 'f', 'ƒ', 'Ï' },
        { 'g', '©', '˝' },
        { 'h', '˙', 'Ó' },
        { 'j', '∆', 'Ô' },
        { 'k', '˚', [[]] },
        { 'l', '¬', 'Ò' },
        { ';', '…', 'Ú' },
        { [[']], 'æ', 'Æ' },
        { 'z', 'Ω', '¸' },
        { 'x', '≈', '˛' },
        { 'c', 'ç', 'Ç' },
        { 'v', '√', '◊' },
        { 'b', '∫', 'ı' },
        { 'n', '˜', '˜' },
        { 'm', 'µ', 'Â' },
        { ',', '≤', '¯' },
        { '.', '≥', '˘' },
        { '/', '÷', '¿' },
    }
    for _, pair in ipairs(mapping) do
        i('<M-' .. pair[1] .. '>', pair[2])
        i('<M-S-' .. pair[1] .. '>', pair[3])
    end
end

return M
