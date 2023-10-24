U = require('util')
U.g.neo_tree_remove_legacy_commands = true
return {
    {
        'lukas-reineke/indent-blankline.nvim',
        main = 'ibl',
        opts = {
            indent = {
                char = '▏',
            },
            scope = {
                show_exact_scope = true,
            },
        },
    },

    {
        'nvim-lualine/lualine.nvim',
        opts = {
            options = {
                theme = 'onelight',
            },
            sections = {
                lualine_a = { 'mode' },
                lualine_b = { { 'filename', path = 1 } },
                lualine_c = { 'diagnostics' },
                lualine_x = { 'filetype' },
                lualine_y = { 'progress', 'location', 'diff' },
                lualine_z = { 'branch' },
            },
        },

    },

    {
        'nvim-neo-tree/neo-tree.nvim',
        dependencies = {
            'MunifTanjim/nui.nvim',
            'nvim-tree/nvim-web-devicons',
            'nvim-lua/plenary.nvim',
        },
        event = 'CmdLineEnter',
        config = {
            sort_case_insensitive = true,
            window = {
                position = 'right',
                width = 80,
            },
            filesystem = {
                follow_current_file = true,
                hijack_netrw_behavior = 'open_current',
                use_libuv_file_watcher = true,
            },
        },
    },

    {
        'folke/noice.nvim',
        dependencies = {
            'rcarriga/nvim-notify',
            'MunifTanjim/nui.nvim',
        },
        config = function()
            require('notify').setup {
                top_down = false,
                stages = 'static',
            }
            require('noice').setup {
                presets = {
                    long_message_to_split = true,
                },
                lsp = {
                    signature = { enabled = false },
                },
            }
        end,
    },

    {
        'navarasu/onedark.nvim',
        config = function()
            local onedark = require('onedark')
            onedark.setup {
                style = 'light',
                highlights = {
                    rainbowcol1 = { fg = 'Black' },
                    rainbowcol2 = { fg = 'DarkGreen' },
                    rainbowcol3 = { fg = 'DarkMagenta' },
                    rainbowcol4 = { fg = 'DarkBlue' },
                    rainbowcol5 = { fg = 'DarkRed' },
                    rainbowcol6 = { fg = 'DarkGray' },
                    IlluminatedWordText = { bg = '#f6d5f5' },
                    IlluminatedWordRead = { bg = '#f6d5f5' },
                    IlluminatedWordWrite = { bg = '#f6d5f5' },
                    -- Spell highlight only add underlines.
                    SpellBad = { fg = 'unset', bg = 'unset' },
                    SpellCap = { fg = 'unset', bg = 'unset' },
                    SpellRare = { fg = 'unset', bg = 'unset' },
                    SpellLocal = { fg = 'unset', bg = 'unset' },
                    -- Fix indent highlight.
                    IblScope = { fg = '#e0e0e0' },
                },
            }
            onedark.load()
        end,
    },

    {
        'folke/todo-comments.nvim',
        dependencies = { 'nvim-lua/plenary.nvim' },
        opts = {
            keywords = {
                FIX = {
                    alt = { 'FIXME', 'Fix me', 'fix me' },
                },
                TODO = {
                    alt = { 'todo', 'Todo' },
                },
            },
        },
    },

    {
        'folke/trouble.nvim',
        event = 'CmdLineEnter',
        dependencies = 'nvim-tree/nvim-web-devicons',
        opts = {
            position = 'right',
            autoclose = true,
        },
    },

    {
        'powerman/vim-plugin-AnsiEsc',
        event = 'VeryLazy',
    },
}

