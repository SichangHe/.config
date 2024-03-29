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
    -- ```python
    -- # Note: keep these two aligned.
    -- original = """1234567890-=_+qwrtyuiop[]\{}|asdfghjkl;':"zxcvbnm,./<>?"""
    -- w_option = """¡™£¢∞§¶•ªº–≠—±œ∑®†¥¨ˆøπ“‘«”’»åß∂ƒ©˙∆˚¬…æÚÆΩ≈ç√∫˜µ≤≥÷¯˘¿"""
    -- for o, w in zip(original, w_option):
    --     print(f"{{{repr(o)}, {repr(w)}}},")
    -- ```
    local option_map = {
        { '1', '¡' },
        { '2', '™' },
        { '3', '£' },
        { '4', '¢' },
        { '5', '∞' },
        { '6', '§' },
        { '7', '¶' },
        { '8', '•' },
        { '9', 'ª' },
        { '0', 'º' },
        { '-', '–' },
        { '=', '≠' },
        { '_', '—' },
        { '+', '±' },
        { 'q', 'œ' },
        { 'w', '∑' },
        { 'r', '®' },
        { 't', '†' },
        { 'y', '¥' },
        { 'u', '¨' },
        { 'i', 'ˆ' },
        { 'o', 'ø' },
        { 'p', 'π' },
        { '[', '“' },
        { ']', '‘' },
        { '\\', '«' },
        { '{', '”' },
        { '}', '’' },
        { '|', '»' },
        { 'a', 'å' },
        { 's', 'ß' },
        { 'd', '∂' },
        { 'f', 'ƒ' },
        { 'g', '©' },
        { 'h', '˙' },
        { 'j', '∆' },
        { 'k', '˚' },
        { 'l', '¬' },
        { ';', '…' },
        { "'", 'æ' },
        { ':', 'Ú' },
        { '"', 'Æ' },
        { 'z', 'Ω' },
        { 'x', '≈' },
        { 'c', 'ç' },
        { 'v', '√' },
        { 'b', '∫' },
        { 'n', '˜' },
        { 'm', 'µ' },
        { ',', '≤' },
        { '.', '≥' },
        { '/', '÷' },
        { '<', '¯' },
        { '>', '˘' },
        { '?', '¿' },
    }
    for _, pair in ipairs(option_map) do
        i('<M-' .. pair[1] .. '>', pair[2])
    end
end

return M
