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
        'ziontee113/icon-picker.nvim',
        event = 'InsertEnter',
        dependencies = { 'stevearc/dressing.nvim' },
        opts = {
            disable_legacy_commands = true,
        },
    },

    {
        'nvim-telescope/telescope.nvim',
        opts = {
            defaults = {
                border = false,
                layout_config = {
                    height = 0.99,
                    width = 0.99,
                }
                ,
            },
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
