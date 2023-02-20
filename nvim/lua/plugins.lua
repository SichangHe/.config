U = require('util')

return function(packer_use)
    packer_use {
        'lewis6991/impatient.nvim',
        config = function()
            require('impatient')
        end,
    }

    local function use(package)
        local requires = package.requires
        if requires == nil then
            requires = {}
        elseif type(requires) == 'string' then
            requires = { requires }
        end
        table.insert(requires, 'lewis6991/impatient.nvim')
        package.requires = requires
        packer_use(package)
    end

    for _, v in ipairs({
        'appear',
        'lazy_plugins',
        'git',
        'ts',
        'lsp',
        'adapter',
        'complete',
    }) do
        U.use(v)(use)
    end

    use {
        'RRethy/vim-illuminate',
        config = function()
            require('illuminate').configure {
                providers = {
                    'lsp',
                    'treesitter',
                },
                filetype_overrides = {
                    markdown = {
                        providers = {
                            'regex',
                        }
                    }
                },
            }
        end
    }

    use {
        'iamcco/markdown-preview.nvim',
        ft = 'markdown',
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
        'nvim-tree/nvim-web-devicons',
        event = 'CmdLineEnter',
        config = function()
            require('nvim-web-devicons').setup {
                default = true
            }
        end,
    }

    use {
        'preservim/vim-markdown',
        ft = 'markdown',
        config = function()
            U.g.vim_markdown_folding_disabled = true
        end,
    }
end
