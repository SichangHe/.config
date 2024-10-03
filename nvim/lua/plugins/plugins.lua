U = require('util')

return {
    {
        'CopilotC-Nvim/CopilotChat.nvim',
        opts = {
            context = 'buffer',
            model = nil
        },
    },

    {
        'ryleelyman/latex.nvim',
        opts = {},
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
        'quarto-dev/quarto-nvim',
        dependencies = {
            'jmbuhr/otter.nvim',
            'nvim-treesitter/nvim-treesitter',
        },
    },

    {
        'MeanderingProgrammer/render-markdown.nvim',
        opts = {
            latex = {
                enabled = false,
            },
            win_options = {
                conceallevel = {
                    -- To fix overriding latex.nvim conceal.
                    rendered = vim.api.nvim_get_option_value('conceallevel', {}),
                },
            },
        },
    },

    {
        'lervag/vimtex',
        -- Old: Help article: <https://www.ejmastnak.com/tutorials/vim-latex/pdf-reader/#refocus-nvim-macos-inverse>.
        -- Sioyek documentation: <https://sioyek-documentation.readthedocs.io/en/latest/usage.html#synctex>.
        init = function()
            U.g.vimtex_view_method = 'sioyek'
        end,
        -- VimTeX cannot be lazy-loaded: <https://github.com/lervag/vimtex?tab=readme-ov-file#installation>
        lazy = false,
    },
}
