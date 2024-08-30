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
        'adelarsq/image_preview.nvim',
        event = 'VeryLazy',
        config = true,
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
        dependencies = { 'SichangHe/nvim-telescope--telescope-media-files.nvim' },
        opts = {
            defaults = {
                border = false,
                layout_config = {
                    height = 0.99,
                    width = 0.99,
                },
                mappings = {
                    n = {
                        ['<C-x>'] = require('telescope.actions').delete_buffer
                    },
                    i = {
                        ['<C-x>'] = require('telescope.actions').delete_buffer
                    },
                },
            },
        },
    },

    {
        'SichangHe/nvim-telescope--telescope-media-files.nvim',
        branch = 'kitty-workaround',
        config = function()
            LazyVim.on_load('telescope.nvim', function()
                local telescope = require('telescope')
                telescope.load_extension('media_files')
                U.key('n', '<Space>fm',
                    telescope.extensions.media_files.media_files,
                    { desc = 'Find Image and Other Media Files' }
                )
            end)
        end,
        lazy = true,
    },

    {
        'altermo/ultimate-autopair.nvim',
        event = { 'InsertEnter', 'CmdlineEnter' },
        -- <https://github.com/altermo/ultimate-autopair.nvim/blob/v0.6/Q%26A.md>
        opts = {
            extensions = {
                filetype = {
                    nft = { 'javascript' }, --Disable because broken.
                },
                cond = {
                    ---Disable in replace mode.
                    cond = function(fn)
                        return fn.get_mode() ~= 'R'
                    end
                }
            },
        },
    },

    {
        'linux-cultist/venv-selector.nvim',
        branch = 'regexp', -- Use this branch for the new version
        opts = {
            settings = {
                options = {
                    notify_user_on_venv_activation = true,
                },
            },
        },
        cmd = 'VenvSelect',
        ft = 'python',
        keys = { { '<leader>cv', '<cmd>:VenvSelect<cr>', desc = 'Select VirtualEnv', ft = 'python' } },
    },
}
