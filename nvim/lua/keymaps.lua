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
    l('s', function()
        U.buf.format()
        U.w()
    end)
    l('<S-o>', ':FzfLua oldfiles<CR>')
    l('o', ':FzfLua files<CR>')
    l(';', ':FzfLua commands<CR>')
    l('.', U.buf.code_action)
    l('<Tab>', ':FzfLua buffers<CR>')
    i('<Space><Space>m', '$$<Left>')
    i('<Space><Space>mm', '$$<CR>$$<Esc><S-o>')
    i('<Space><Space>i', '<Cmd>IconPickerInsert<CR>')
    l('f', ':FzfLua blines<CR>')
    l('ff', ':FzfLua grep<CR>')
    l('j', ':FzfLua jumps<CR>')
    l('l', ':lua ')
    l('dv', ':Diffview')
    l('rt', U.lsp.codelens.run)
    l('db', ":lua require('dapui').toggle()<CR>")
    l('ld', ':Lspsaga show_cursor_diagnostics<CR>')
    l('co', function()
        return ':!co . && co --goto %:' .. current_line() .. '<CR>'
    end, { expr = true })
    l('vvvv', function()
        if U.set.scrollbind:get() then
            -- Toggle off.
            return ':q<CR>:set noscrollbind<CR>'
        end
        local line = current_line() -- Remember current line.
        return 'gg:set scrollbind<CR><C-w>v' -- Go to top, bind, split.
            .. ':set noscrollbind<CR><C-f>2<C-e>' -- Unbind, down 1 page.
            .. ':set scrollbind<CR><C-w>h' -- Bind, switch to left split.
            .. line .. 'gg<C-w>l' -- Back to previous line, switch right.
    end, {
        expr = true,
        desc = 'Toggle two column view.'
    })
    l('rrrr', ':lua AllConfig()<CR>')
end

return M
