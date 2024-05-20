U = require("util")

-- U.lsp.set_log_level('debug') -- debug LSP

return {
    {
        'stevearc/conform.nvim',
        opts = {
            formatters = {
                latexindent = {
                    prepend_args = {
                        '-l',
                        U.expand('~/.config/latexindent_config.yaml'),
                    },
                },
                shfmt = {
                    prepend_args = { '-i', '4', '-bn', '-ci', '-sr' },
                },
            },
            formatters_by_ft = {
                bib = { 'bibtex-tidy' },
                markdown = { 'markdownlint' },
                python = { 'ruff_format' },
                lua = {},
                sh = { 'shfmt' },
                tex = { 'latexindent' },
                -- Prettierd
                handlebars = { 'prettierd' },
                yaml = { 'prettierd' },
            },
        },
    },

    {
        'akinsho/flutter-tools.nvim',
        ft = { 'dart' },
        dependencies = { 'stevearc/dressing.nvim', 'nvim-lua/plenary.nvim' },
        config = true,
    },

    {
        'ray-x/go.nvim',
        dependencies = {
            'ray-x/guihua.lua',
            'neovim/nvim-lspconfig',
            'nvim-treesitter/nvim-treesitter',
        },
        event = { "CmdlineEnter" },
        ft = { "go", 'gomod' },
        opts = {
            lsp_cfg = true,
        },
        build = ':lua require("go.install").update_all_sync()',
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
        event = 'InsertEnter',
        dependencies = { 'neovim/nvim-lspconfig' },
        opts = {},
    },

    {
        'williamboman/mason.nvim',
        opts = function(_, opts)
            for _, program in ipairs({
                'bibtex-tidy',
                'prettierd',
                'ruff',
            }) do
                table.insert(opts.ensure_installed, program)
            end

            -- Override and not to install with Mason.
            local to_remove = {
                stylua = true,
            }
            for index, program in ipairs(opts.ensure_installed) do
                if to_remove[program] then
                    table.remove(opts.ensure_installed, index)
                end
            end
        end,
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
                basedpyright = {
                    basedpyright = {
                        typeCheckingMode = 'standard',
                    },
                },
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
                solargraph = {},
                svelte = {},
                tailwindcss = {},
                taplo = {},
                tsserver = {},
                vale_ls = {},
            }
            local ensure = U.tbl_keys(servers)
            for _, v in ipairs({
                -- Other servers configured with extensions.
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
        'folke/neodev.nvim',
        lazy = true,
    },

    {
        'mrcjkb/rustaceanvim',
        opts = {
            tools = {
                hover_actions = {
                    border = 'none',
                    auto_focus = true,
                },
            },
        }
    },
}
