U = require('util')
local M = {}
local one_meg = 1024 * 1024

function M.set()
    U.auto_cmd('TextYankPost', 'silent! lua vim.highlight.on_yank()') -- Highlight on yank.
    U.auto_cmd('CursorHold', 'silent! wa')                            -- Autosave on no action.
    U.auto_cmd('BufReadPost', [[
        if line("'\"") >= 1 && line("'\"") <= line("$") && &ft !~# 'commit'
        exe "normal! g`\""
        endif
    ]]) -- Restore cursor location in file.
    U.auto_call({ 'BufReadPre', 'FileReadPre' }, function()
        local size = U.fn.getfsize(U.current_buf_path())
        if size > one_meg or size == -2 then
            U.b.large_buf = true
            U.cmd('syntax off')
            U.cmd_if_exists('IlluminatePauseBuf')     -- disable vim-illuminate
            U.cmd_if_exists('IndentBlanklineDisable') -- disable indent-blankline.nvim
            U.set_buf.wrap = false
            U.set_buf.foldmethod = 'manual'
            U.set_buf.spell = false
        else
            U.b.large_buf = false
        end
    end)
end

return M
