U = require('util')
local auto = U.auto
local M = {}

function M.set()
    auto('TextYankPost', 'silent! lua vim.highlight.on_yank()') -- Highlight on yank.
    auto('CursorHold', 'silent! wa') -- Autosave on no action.
    auto('BufReadPost', [[
        if line("'\"") >= 1 && line("'\"") <= line("$") && &ft !~# 'commit'
        exe "normal! g`\""
        endif
    ]]) -- Restore cursor location in file.
end

return M