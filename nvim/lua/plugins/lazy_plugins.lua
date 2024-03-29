U = require('util')

return {
    {
        'numToStr/Comment.nvim',
        event = 'VeryLazy',
        config = true,
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
        'mikesmithgh/kitty-scrollback.nvim',
        cmd = { 'KittyScrollbackGenerateKittens', 'KittyScrollbackCheckHealth' },
        event = { 'User KittyScrollbackLaunch' },
        config = true,
        build = ':KittyScrollbackGenerateKittens',
    },

    {
        'Wansmer/sibling-swap.nvim',
        dependencies = { 'nvim-treesitter/nvim-treesitter' },
        event = { 'InsertEnter' },
        config = function()
            local swap = require('sibling-swap')
            swap.setup {
                use_default_keymaps = false,
            }
            U.key('i', '<C-,>', swap.swap_with_left)
            U.key('i', '<C-.>', swap.swap_with_right)
            U.key('i', '<C-S-,>', swap.swap_with_left_with_opp)
            U.key('i', '<C-S-.>', swap.swap_with_right_with_opp)
        end,
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
}
