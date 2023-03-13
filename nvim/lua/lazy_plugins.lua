return {
    {
        'numToStr/Comment.nvim',
        event = 'VeryLazy',
        config = true,
    },

    {
        'saecki/crates.nvim',
        event = { 'BufRead Cargo.toml' },
        dependencies = { 'hrsh7th/nvim-cmp', 'nvim-lua/plenary.nvim' },
        config = function()
            require('crates').setup {}
            require('cmp').setup.buffer {
                sources = {
                    { name = 'crates' },
                },
            }
        end,
    },

    {
        'ibhagwan/fzf-lua',
        event = 'CmdLineEnter',
        dependencies = { 'nvim-tree/nvim-web-devicons' },
        opts = {
            fullscreen = true,
            previewers = {
                builtin = {
                    extensions = {
                        ["png"] = { "viu" },
                        ["jpg"] = { "viu" },
                    },
                },
            },
        },
    },

    {
        'windwp/nvim-autopairs',
        event = 'InsertEnter',
        config = true,
    },

    {
        'machakann/vim-swap',
        event = 'InsertEnter',
    },
}
