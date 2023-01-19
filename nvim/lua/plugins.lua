U = require('util')

return function(use)
    for _, v in ipairs({
        'appear',
        'lazy_plugins',
        'git',
        'ts',
        'lsp',
        'complete',
    }) do
        U.use(v)(use)
    end

    use {
        'iamcco/markdown-preview.nvim',
        run = 'cd app && yarn',
        config = function()
            U.g.mkdp_auto_close = false
            U.g.mkdp_preview_options = {
                disable_filename = true,
                sync_scroll_type = 'relative',
            }
            U.g.mkdp_markdown_css = U.conf_loc .. 'markdown.css'
            U.g.mkdp_page_title = '${name}'
        end,
    }

    use {
        'preservim/vim-markdown',
        config = function()
            U.g.vim_markdown_folding_disabled = true
        end,
    }
end
