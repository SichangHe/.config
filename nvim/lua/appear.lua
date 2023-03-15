return {
    {
        'lukas-reineke/indent-blankline.nvim',
        opts = {
            show_current_context = true,
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
                    IlluminatedWordText = { bg = '#fff0fb' },
                    IlluminatedWordRead = { bg = '#fff0fb' },
                    IlluminatedWordWrite = { bg = '#fff0fb' },
                    -- Spell highlight only add underlines.
                    SpellBad = { fg = 'unset', bg = 'unset' },
                    SpellCap = { fg = 'unset', bg = 'unset' },
                    SpellRare = { fg = 'unset', bg = 'unset' },
                    SpellLocal = { fg = 'unset', bg = 'unset' },
                    -- Fix indent highlight.
                    IndentBlanklineContextChar = { fg = '#e0e0e0' },
                },
            }
            onedark.load()
        end,
    },

    {
        'petertriho/nvim-scrollbar',
        config = true,
    },

    {
        'folke/todo-comments.nvim',
        dependencies = { 'nvim-lua/plenary.nvim' },
        opts = {
            keywords = {
                FIX = {
                    alt = { 'Fix me', 'fix me' },
                },
                TODO = {
                    alt = { 'todo', 'Todo' },
                },
            },
        },
    },

    {
        "folke/trouble.nvim",
        event = 'CmdLineEnter',
        dependencies = "nvim-tree/nvim-web-devicons",
        opts = {
            position = 'right',
            autoclose = true,
        },
    },
}
