return function(use)
    use {
        'lukas-reineke/indent-blankline.nvim',
        config = function()
            require('indent_blankline').setup {
                show_current_context = true,
            }
        end,
    }

    use {
        'nvim-lualine/lualine.nvim',
        config = function()
            require('lualine').setup {
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
            }
        end,
    }

    use {
        'folke/noice.nvim',
        requires = {
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
    }

    use {
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
                    IlluminatedWordText = { bg = '#e6e6e6' },
                    IlluminatedWordRead = { bg = '#e6e6e6' },
                    IlluminatedWordWrite = { bg = '#e6e6e6' },
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
    }

    use {
        "folke/trouble.nvim",
        event = 'CmdLineEnter',
        requires = "nvim-tree/nvim-web-devicons",
        config = function()
            require("trouble").setup {
                position = 'right',
                autoclose = true,
            }
        end,
    }
end