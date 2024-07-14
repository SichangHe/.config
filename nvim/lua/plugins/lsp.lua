U = require("util")

-- U.lsp.set_log_level('debug') -- debug LSP

local function mdbook_ls_setup(capabilities)
    local lspconfig = require('lspconfig')
    local function execute_command_with_params(params)
        local clients = lspconfig.util.get_lsp_clients {
            bufnr = vim.api.nvim_get_current_buf(),
            name = 'mdbook_ls',
        }
        for _, client in ipairs(clients) do
            client.request('workspace/executeCommand', params, nil, 0)
        end
    end
    local function open_preview()
        local params = {
            command = 'open_preview',
            arguments = { "127.0.0.1:33000", vim.api.nvim_buf_get_name(0) },
        }
        execute_command_with_params(params)
    end
    local function stop_preview()
        local params = {
            command = 'stop_preview',
            arguments = {},
        }
        execute_command_with_params(params)
    end

    require('lspconfig.configs')['mdbook_ls'] = {
        default_config = {
            cmd = { 'mdbook-ls' },
            filetypes = { 'markdown' },
            root_dir = lspconfig.util.root_pattern('book.toml'),
        },
        commands = {
            MDBookLSOpenPreview = {
                open_preview,
                description = 'Open mdBook-LS preview',
            },
            MDBookLSStopPreview = {
                stop_preview,
                description = 'Stop mdBook-LS preview',
            },
        },
        docs = {
            description = [[The mdBook Language Server for previewing mdBook projects live.]],
        },
    }
    lspconfig['mdbook_ls'].setup {
        capabilities = capabilities,
    }
end

local function natural_syntax_ls_setup(capabilities)
    local lspconfig = require('lspconfig')
    require('lspconfig.configs')['natural_syntax_ls'] = {
        default_config = {
            cmd = { 'natural-syntax-ls' },
            filetypes = { 'text' },
            single_file_support = true,
        },
        docs = {
            description = [[The Natural Syntax Language Server for highlighting parts of speech.]],
        },
    }
    lspconfig['natural_syntax_ls'].setup {
        capabilities,
        init_options = {
            token_map_update = {
                CC = vim.NIL,
                DT = vim.NIL,
                IN = vim.NIL,
                PDT = vim.NIL,
                TO = vim.NIL,
            },
        },
    }
end

return {
    {
        'stevearc/conform.nvim',
        opts = {
            formatters = {
                fmtm = {
                    command = "fmtm",
                },
                latexindent = {
                    prepend_args = {
                        '-l',
                        U.expand('~/.config/latexindent_config.yaml'),
                    },
                },
                markdownlint_cli2 = {
                    prepend_args = {
                        '--config',
                        U.expand('~/.config/.markdownlint-cli2.jsonc'),
                    },
                },
                shfmt = {
                    prepend_args = { '-i', '4', '-bn', '-ci', '-sr' },
                },
            },
            formatters_by_ft = {
                bib = { 'bibtex-tidy' },
                markdown = { 'markdownlint-cli2', "fmtm" },
                lua = { 'lua_ls' },
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
        'ionide/Ionide-vim',
        dependencies = { 'neovim/nvim-lspconfig' },
        event = 'VeryLazy',
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
        'neovim/nvim-lspconfig',
        opts = {
            ---@type vim.diagnostic.Opts
            diagnostics = {
                virtual_text = {
                    spacing = 1,
                    source = false,
                },
            },
            setup = {
                pyright = function() -- Disable Pyright.
                    return true
                end,
                rust_analyzer = function() -- Prevent double setup.
                    return true
                end,
            },
        },
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
                        configurationSources = { 'mypy' },
                        plugins = {
                            autopep8 = { enabled = false },
                            jedi_completion = {
                                eager = true,
                                fuzzy = true,
                            },
                            -- Use BasedPyright.
                            jedi_definition = { enabled = false },
                            jedi_references = { enabled = false },
                            mccabe = { enabled = false },
                            mypy = {
                                enabled = true,
                                report_progress = true,
                            },
                            pycodestyle = { enabled = false },
                            pyflakes = { enabled = false },
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
            lspconfig.gleam.setup {
                capabilities = capabilities,
            }
            lspconfig.sourcekit.setup {
                capabilities = capabilities,
            }
            lspconfig.ruff.setup {
                capabilities = capabilities,
            }

            mdbook_ls_setup(capabilities)
            natural_syntax_ls_setup(capabilities)
        end,

    },

    {
        'mrcjkb/rustaceanvim',
        opts = function(_, opts)
            vim.tbl_extend('force', opts, {
                tools = {
                    hover_actions = {
                        border = 'none',
                        auto_focus = true,
                    },
                },
            })
        end
    },
}
