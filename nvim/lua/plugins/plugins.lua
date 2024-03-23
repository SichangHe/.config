U = require('util')

return {
    {
        'ggandor/leap.nvim',
        dependencies = { 'tpope/vim-repeat' },
        config = function()
            require('leap').create_default_mappings()
        end,
    },

    {
        'RRethy/vim-illuminate',
        event = 'VeryLazy',
        config = function()
            require('illuminate').configure {
                providers = {
                    'lsp',
                    'treesitter',
                },
                filetype_overrides = {
                    latex = {
                        providers = {
                            'treesitter',
                            'regex',
                        }
                    },
                    markdown = {
                        providers = {
                            'regex',
                        }
                    }
                },
            }
        end,
    },

    {
        'iamcco/markdown-preview.nvim',
        ft = 'markdown',
        build = U.fn["mkdp#util#install"],
        config = function()
            U.g.mkdp_auto_close = false
            U.g.mkdp_preview_options = {
                disable_filename = true,
                sync_scroll_type = 'relative',
            }
            U.g.mkdp_markdown_css = U.conf_loc .. 'markdown.css'
            U.g.mkdp_page_title = '${name}'
        end,
    },

    {
        'nvim-tree/nvim-web-devicons',
        opts = { default = true },
    },

    {
        'preservim/vim-markdown',
        ft = 'markdown',
        config = function()
            U.g.vim_markdown_folding_disabled = true
        end,
    },

    {
        'lervag/vimtex',
        -- Old: Help article: <https://www.ejmastnak.com/tutorials/vim-latex/pdf-reader/#refocus-nvim-macos-inverse>.
        -- Sioyek documentation: <https://sioyek-documentation.readthedocs.io/en/latest/usage.html#synctex>.
        config = function()
            U.g.vimtex_view_method = 'sioyek'
        end,
    },
}
