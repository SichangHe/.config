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
            U.g.vim_markdown_math = true
        end,
    },

    {
        'lervag/vimtex',
        -- Old: Help article: <https://www.ejmastnak.com/tutorials/vim-latex/pdf-reader/#refocus-nvim-macos-inverse>.
        -- Sioyek documentation: <https://sioyek-documentation.readthedocs.io/en/latest/usage.html#synctex>.
        init = function()
            U.g.vimtex_view_method = 'sioyek'
            U.g.vimtex_compiler_method = 'tectonic'
            U.g.vimtex_compiler_method = 'generic'
            U.g.vimtex_compiler_generic = {
                -- Well, it always returns 0 (succeeds)…
                command = [[watchexec -e tex -e bib "
                    echo vimtex_compiler_callback_compiling &&
                    tectonic main.tex -Z continue-on-errors --keep-intermediates --synctex --keep-logs &&
                    echo vimtex_compiler_callback_success ||
                    echo vimtex_compiler_callback_failure
                "]],
            }
        end,
        -- VimTeX cannot be lazy-loaded: <https://github.com/lervag/vimtex?tab=readme-ov-file#installation>
        lazy = false,
    },
}
