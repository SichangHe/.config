return {
    {
        'akinsho/flutter-tools.nvim',
        ft = { 'dart' },
        dependencies = { 'stevearc/dressing.nvim', 'nvim-lua/plenary.nvim' },
        config = true,
    },

    {
        'glepnir/lspsaga.nvim',
        event = 'VeryLazy',
        dependencies = { 'nvim-tree/nvim-web-devicons' },
        opts = {
            lightbulb = {
                enable_in_insert = false,
                virtual_text = false,
            },
            symbol_in_winbar = {
                enable = false,
            },
            ui = {
                border = 'none',
            },
        },
    },

    {
        'ray-x/lsp_signature.nvim',
        event = 'VeryLazy',
        dependencies = { 'neovim/nvim-lspconfig' },
        config = true,
    },

    {
        'williamboman/mason.nvim',
        config = true,
    },

    {
        'williamboman/mason-lspconfig.nvim',
        event = 'VeryLazy',
        dependencies = {
            'williamboman/mason.nvim',
            'neovim/nvim-lspconfig',
            'hrsh7th/cmp-nvim-lsp',
        },
        config = function()
            local servers = {
                bashls = {},
                clangd = {},
                cssls = {},
                elixirls = {},
                emmet_ls = {},
                html = {},
                jsonls = {},
                julials = {},
                lua_ls = {
                    Lua = {
                        workspace = {
                            checkThirdParty = false,
                        },
                    },
                },
                pylsp = {
                    pylsp = {
                        configurationSources = { 'mypy', 'ruff' },
                        plugins = {
                            autopep8 = { enabled = false },
                            jedi_completion = {
                                eager = true,
                                fuzzy = true,
                            },
                            mccabe = { enabled = false },
                            mypy = {
                                enabled = true,
                                report_progress = true,
                            },
                            pycodestyle = { enabled = false },
                            pyflakes = { enabled = false },
                            ruff = { enabled = true },
                            yapf = { enabled = false },
                        },
                    },
                },
                pyright = {},
                solargraph = {},
                svelte = {},
                tailwindcss = {},
                taplo = {},
                tsserver = {},
            }
            local ensure = U.tbl_keys(servers)
            for _, v in ipairs({
                -- Other servers configured with extensions.
                'rust_analyzer',
            }) do
                table.insert(ensure, v)
            end
            local capabilities = require('cmp_nvim_lsp')
                .default_capabilities(U.lsp.protocol.make_client_capabilities())
            require('mason-lspconfig').setup {
                ensure_installed = ensure,
            }
            local lspconfig = require('lspconfig')
            require('mason-lspconfig').setup_handlers {
                function(name)
                    if servers[name] then
                        local conf = lspconfig[name]
                        conf.setup {
                            autostart = servers[name].autostart,
                            capabilities = capabilities,
                            settings = servers[name],
                        }
                        -- Disable LSP on large buffer.
                        local try_add = conf.manager.try_add
                        conf.manager.try_add = function(bufnr)
                            if not U.b.large_buf then
                                return try_add(bufnr)
                            end
                        end
                    end
                end
            }
            -- Other LSPs.
            lspconfig["sourcekit"].setup {
                capabilities = capabilities,
                settings = {},
            }
        end,
    },

    {
        'jay-babu/mason-null-ls.nvim',
        event = 'VeryLazy',
        dependencies = { 'williamboman/mason.nvim', 'jose-elias-alvarez/null-ls.nvim' },
        opts = {
            ensure_installed = {
                'black',
                'isort',
                'markdownlint',
                'prettierd',
            },
        },
    },

    {
        'folke/neodev.nvim',
        config = true,
    },

    {
        'jose-elias-alvarez/null-ls.nvim',
        event = 'VeryLazy',
        dependencies = { 'nvim-lua/plenary.nvim' },
        config = function()
            local null_ls = require('null-ls')
            null_ls.setup {
                sources = {
                    -- LaTeX
                    null_ls.builtins.formatting.bibclean,
                    null_ls.builtins.formatting.latexindent,
                    -- prettier
                    null_ls.builtins.formatting.prettierd.with {
                        filetypes = {
                            "css",
                            "scss",
                            "svelte",
                            "less",
                            "js",
                            "json",
                            "jsonc",
                        },
                    },
                    null_ls.builtins.formatting.isort,
                    null_ls.builtins.formatting.black,
                    -- markdownlint
                    null_ls.builtins.diagnostics.markdownlint_cli2.with {
                        command = 'markdownlint-cli2-config',
                        args = {
                            U.expand('~/.config/.markdownlint-cli2.jsonc'),
                            '$FILENAME'
                        },
                    },
                    null_ls.builtins.formatting.markdownlint.with {
                        command = 'markdownlint-cli2-config',
                        args = {
                            U.expand('~/.config/fix.markdownlint-cli2.jsonc'),
                            '$FILENAME'
                        },
                    },
                },
            }
        end,
    },

    {
        'simrat39/rust-tools.nvim',
        ft = { 'rust' },
        dependencies = { 'neovim/nvim-lspconfig', 'mfussenegger/nvim-dap' },
        config = function()
            local rt = require('rust-tools')
            rt.setup {
                dap = {
                    adapter = {
                        type = "server",
                        port = "${port}",
                        host = "127.0.0.1",
                        executable = {
                            command = 'codelldb',
                            args = { "--port", "${port}" },
                        },
                    },
                },
                server = {
                    settings = {
                        ['rust-analyzer'] = {
                            check = {
                                command = 'clippy',
                            },
                        },
                    },
                    on_attach = function(_, buf_n)
                        U.key('n', 'K', rt.hover_actions.hover_actions, {
                            buffer = buf_n,
                        })
                    end
                },
                tools = {
                    hover_actions = {
                        border = 'none',
                        auto_focus = true,
                    },
                },
            }
        end,
    },
}
