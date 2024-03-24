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
    i('<Space><Space>m', '$$<Left>')
    i('<Space><Space>mm', '$$<CR>$$<Esc><S-o>')
    i('<Space><Space>i', '<Cmd>IconPickerInsert<CR>')
    l('lua', ':lua ')
    l('dv', ':Diffview')
    l('db', ":lua require('dapui').toggle()<CR>")
    l('co', function()
        return ':!co . && co --goto %:' .. current_line() .. '<CR>'
    end, { expr = true, desc = 'Open in vscode' })
    l('vvvv', function()
        return (U.set.scrollbind:get() and
                -- Toggle off: close split, unbind, enable relative line number.
                ':q<CR>:set noscrollbind<CR>:set relativenumber<CR>' or
                (':set norelativenumber<CR>'         -- Disable relative line number.
                    .. 'gg:set scrollbind<CR><C-w>v' -- Go to top, bind, split.
                    .. ':set noscrollbind<CR><C-f>'  -- Unbind, down 1 page.
                    .. ':set scrollbind<CR>'))       -- Bind.
            .. current_line() .. 'gg'                -- Back to previous line.
    end, {
        expr = true,
        desc = 'Toggle two column view.'
    })
    l('rrrr', ':lua AllConfig()<CR>')
end

return M
