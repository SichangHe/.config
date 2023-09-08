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
        'ziontee113/icon-picker.nvim',
        event = 'InsertEnter',
        dependencies = { 'stevearc/dressing.nvim' },
        opts = {
            disable_legacy_commands = true,
        },
    },

    {
        'altermo/ultimate-autopair.nvim',
        event = { 'InsertEnter', 'CmdlineEnter' },
        config = true,
    },

    {
        'machakann/vim-swap',
        event = 'InsertEnter',
    },
}
