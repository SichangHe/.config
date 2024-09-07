U = require('util')
U.g.neo_tree_remove_legacy_commands = true
return {
    {
        'rickhowe/diffchar.vim',
        lazy = false,
    },

    {
        'JellyApple102/easyread.nvim',
        opts = {
            hlgroupOptions = { bold = true },
            fileTypes = { 'markdown', 'tex', 'text' },
        },
    },

    {
        'lukas-reineke/indent-blankline.nvim',
        config = function()
            local highlight = {
                "IblIndentEven",
                "IblIndentOdd",
            }
            require("ibl").setup {
                indent = {
                    highlight = highlight,
                },
                scope = {
                    show_exact_scope = true,
                },
                whitespace = {
                    highlight = highlight,
                },
            }
        end,
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
        opts = {
            sort_case_insensitive = true,
            window = {
                position = 'right',
                width = 40,
                mappings = {
                    ["<Space>p"] = "image_preview",
                },
            },
            filesystem = {
                follow_current_file = { enabled = true },
                hijack_netrw_behavior = 'open_current',
                use_libuv_file_watcher = true,
                commands = {
                    image_preview = function(state)
                        local node = state.tree:get_node()
                        if node.type == "file" then
                            require("image_preview").PreviewImage(node.path)
                        end
                    end,

                },
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
            ---@diagnostic disable-next-line: missing-fields
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
        'brenoprata10/nvim-highlight-colors',
        opts = {},
        event = 'BufReadPre',
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
                    LspReferenceText = { bg = '#f6d5f5' },
                    LspReferenceRead = { bg = '#f6d5f5' },
                    LspReferenceWrite = { bg = '#f6d5f5' },
                    -- Spell highlight only add underlines.
                    SpellBad = { fg = 'unset', bg = 'unset' },
                    SpellCap = { fg = 'unset', bg = 'unset' },
                    SpellRare = { fg = 'unset', bg = 'unset' },
                    SpellLocal = { fg = 'unset', bg = 'unset' },
                    -- Fix indent highlight.
                    IblIndentEven = { fg = '#f0f0f0', bg = '#f0f0f0' },
                    IblIndentOdd = { fg = '#f0f0f0' },
                    IblIdent = { fg = '#f0f0f0' },
                    IblScope = { fg = '#a0a1a7' },
                    -- VimTex conceal.
                    Conceal = { fg = '#333436' },
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
