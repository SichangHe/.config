local M = {}
M.api = vim.api
M.auto = function(event, command)
    vim.api.nvim_create_autocmd(event, { command = command })
end
M.buf = vim.lsp.buf
M.cmd = vim.cmd
M.fn = vim.fn
M.g = vim.g
M.lsp = vim.lsp
M.key = vim.keymap.set
M.set = vim.opt
M.tbl_keys = vim.tbl_keys
M.use = function(module)
    package.loaded[module] = nil
    return require(module)
end

M.conf_loc = M.fn.expand('~/.config/nvim/')

return M
