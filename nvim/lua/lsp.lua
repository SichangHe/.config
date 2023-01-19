return function(use)
    use {
        'williamboman/mason.nvim',
        config = function()
            require('mason').setup {}
        end,
    }

    use {
        'williamboman/mason-lspconfig.nvim',
        requires = {
            'williamboman/mason.nvim',
            'hrsh7th/cmp-nvim-lsp',
        },
        config = function()
            local servers = {
                clangd = {},
                elixirls = {},
                jsonls = {},
                julials = {},
                sumneko_lua = {},
                pyright = {},
                solargraph = {},
                tsserver = {},
            }
            local ensure = U.tbl_keys(servers)
            table.insert(ensure, 'rust_analyzer')
            local capabilities = require('cmp_nvim_lsp')
                .default_capabilities(U.lsp.protocol.make_client_capabilities())
            require('mason-lspconfig').setup {
                ensure_installed = ensure,
            }
            require('mason-lspconfig').setup_handlers {
                function(name)
                    require('lspconfig')[name].setup {
                        capabilities = capabilities,
                        settings = servers[name],
                    }
                end
            }
        end,
    }

    use {
        'jay-babu/mason-null-ls.nvim',
        requires = { 'williamboman/mason.nvim', 'jose-elias-alvarez/null-ls.nvim' },
        config = function()
            require('mason-null-ls').setup({
                ensure_installed = { 'black', 'isort', 'markdownlint' },
            })
        end,
    }

    use {
        'folke/neodev.nvim',
        config = function()
            require('neodev').setup {}
        end,
    }

    use {
        'jose-elias-alvarez/null-ls.nvim',
        requires = { 'nvim-lua/plenary.nvim' },
        config = function()
            local null_ls = require('null-ls')
            null_ls.setup {
                sources = {
                    -- isort && black
                    null_ls.builtins.formatting.isort,
                    null_ls.builtins.formatting.black,
                    -- markdownlint
                    null_ls.builtins.diagnostics.markdownlint,
                    null_ls.builtins.formatting.markdownlint,
                },
            }
        end,
    }

    use {
        'neovim/nvim-lspconfig',
        requires = {
            'williamboman/mason-lspconfig.nvim',
            'folke/neodev.nvim',
        },
    }

    use {
        'simrat39/rust-tools.nvim',
        requires = { 'neovim/nvim-lspconfig', 'mfussenegger/nvim-dap' },
        config = function()
            require('rust-tools').setup {
                server = {
                    settings = {
                        ['rust-analyzer'] = {
                            check = {
                                command = 'clippy',
                            },
                        },
                    },
                },
            }
        end,
    }
end
