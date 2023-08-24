U = require('util')
local util = vim.lsp.util

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
        local params = util.make_position_params()
        vim.lsp.buf_request_all(0, 'textDocument/hover', params, function(responses)
            local kind = 'markdown'
            local lines = {}
            for _, response in pairs(responses) do
                local result = response.result
                if result and result.contents and result.contents.value then
                    local contents = result.contents
                    if contents.kind and result.contents.kind ~= 'markdown' then
                        kind = contents.kind
                    end
                    local new = util.convert_input_to_markdown_lines(contents)
                    new = util.trim_empty_lines(new or {})
                    print(lines)
                    print(new)
                    lines = U.fn.extend(lines, new)
                end
            end

            local _, row = unpack(vim.fn.getpos('.'))
            row = row - 1
            local lineDiag = vim.diagnostic.get(0, { lnum = row })
            if #lineDiag > 0 then
                for _, d in pairs(lineDiag) do
                    if d.message then
                        table.insert(lines, string.format('[%s] - %s', d.source, d.message))
                    end
                end
            end
            for _, l in pairs(lines) do
                l = l:gsub('\r', '')
            end

            if not vim.tbl_isempty(lines) then
                done({ lines = lines, filetype = kind })
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
