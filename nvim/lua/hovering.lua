U = require('util')

-- Copied from
-- <https://github.com/lewis6991/hover.nvim/issues/34#issuecomment-1625662866>
-- and
-- <https://github.com/WillEhrendreich/nvimconfig/blob/c7d8aed0291ee74887dd3a4ea512398a406b82a6/lua/plugins/hover.lua>.
local LSPWithDiagSource = {
    name = 'LSPWithDiag',
    priority = 1000,
    enabled = function()
        return true
    end,
    execute = function(done)
        local util = vim.lsp.util

        local params = util.make_position_params()
        local lines = {}
        vim.lsp.buf_request_all(0, 'textDocument/hover', params, function(responses)
            local lang = 'markdown'
            for _, response in pairs(responses) do
                if response.result and response.result.contents then
                    lang = response.result.contents.language or 'markdown'
                    lines = util.convert_input_to_markdown_lines(response.result.contents or
                        { kind = 'markdown', value = '' })
                    lines = util.trim_empty_lines(lines or {})
                end
            end

            local unused
            local _, row = unpack(vim.fn.getpos('.'))
            row = row - 1
            local lineDiag = vim.diagnostic.get(0, { lnum = row })
            if #lineDiag > 0 then
                for _, d in pairs(lineDiag) do
                    if d.message then
                        lines = util.trim_empty_lines(util.convert_input_to_markdown_lines({
                            language = lang,
                            value = string.format('[%s] - %s', d.source, d.message),
                        }, lines or {}))
                    end
                end
            end
            for _, l in pairs(lines) do
                l = l:gsub('\r', '')
            end

            if not vim.tbl_isempty(lines) then
                done({ lines = lines, filetype = 'markdown' })
                return
            end
            done()
        end)
    end,
}

return {
    'lewis6991/hover.nvim',
    config = function()
        local hover = require('hover')
        hover.setup {
            init = function()
                hover.register(LSPWithDiagSource)
                require('hover.providers.dictionary')
            end,
            preview_opts = {
                border = nil,
            },
            -- Whether the contents of a currently open hover window should be moved
            -- to a :h preview-window when pressing the hover keymap.
            preview_window = false,
            title = true,
        }
        U.key('n', 'K', hover.hover, { desc = 'hover.nvim' })
        U.key('n', 'gK', hover.hover_select, { desc = 'hover.nvim select' })
    end,
}
