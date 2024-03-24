local M = {}
M.api = vim.api
M.auto_call = function(event, callback, args)
    args = args or {}
    args['callback'] = callback
    vim.api.nvim_create_autocmd(event, args)
end
M.auto_cmd = function(event, command, args)
    args = args or {}
    args['command'] = command
    vim.api.nvim_create_autocmd(event, args)
end
M.b = vim.b
M.buf = vim.lsp.buf
M.cmd = vim.cmd
M.cmd_if_exists = function(expr)
    if vim.fn.exists(':' .. expr:gmatch('%w+')()) > 0 then
        vim.cmd(expr)
    end
end
M.current_buf_path = function() return M.api.nvim_buf_get_name(0) end
M.fn = vim.fn
M.expand = M.fn.expand
M.fs_stat = vim.loop.fs_stat
M.g = vim.g
M.lsp = vim.lsp
M.key = vim.keymap.set
M.del_key = vim.keymap.del
M.rtp = vim.opt.rtp
M.set = vim.opt
M.set_buf = vim.opt
M.tbl_keys = vim.tbl_keys
M.use = function(module)
    package.loaded[module] = nil
    return require(module)
end

M.break_undo = function()
    M.cmd [[let &undolevels = &undolevels]]
end
M.conf_loc = M.expand('~/.config/nvim/')
M.w = function()
    M.cmd [[w]]
end

return M
