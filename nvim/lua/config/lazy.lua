local lazypath = vim.fn.stdpath("data") .. "/lazy/lazy.nvim"
if not vim.loop.fs_stat(lazypath) then
    -- bootstrap lazy.nvim
    vim.fn.system({ "git", "clone", "--filter=blob:none", "https://github.com/folke/lazy.nvim.git", "--branch=stable",
        lazypath })
end
vim.opt.rtp:prepend(vim.env.LAZY or lazypath)

require("lazy").setup({
    spec = {
        {
            "LazyVim/LazyVim",
            opts = {
                colorscheme = 'onedark',
            },
            import = "lazyvim.plugins"
        },
        { import = 'lazyvim.plugins.extras.editor.leap' },
        { import = 'lazyvim.plugins.extras.lang.rust' },
        { import = 'lazyvim.plugins.extras.lsp.none-ls' },
        { import = "plugins" },
    },
})
