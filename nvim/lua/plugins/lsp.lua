U = require("util")

-- U.lsp.set_log_level('debug') -- debug LSP

local servers = {
    basedpyright = {
        settings = {
            basedpyright = {
                typeCheckingMode = 'basic',
            },
        },
    },
    bashls = {},
    clangd = {},
    cssls = {},
    elixirls = {},
    emmet_ls = {},
    gleam = {
        mason = false,
    },
    html = {},
    jsonls = {},
    julials = {},
    ltex = {
        autostart = false, -- LTeX is too heavy for regular use.
        settings = {
            ltex = {
                dictionary = {
                    ['en-US'] = {}, -- Initialized below.
                },
            },
        },
        on_init = function(client, bufnr)
            _ = bufnr
            local spell_file_name = U.conf_loc .. 'spell/en.utf-8.add'
            local spell_file = io.open(spell_file_name, 'r')
            if spell_file then
                local dict = client.config.settings.ltex.dictionary['en-US']
                for line in spell_file:lines() do
                    table.insert(dict, line)
                end
                spell_file:close()
            end
        end,
    },
    mdbook_ls = {},
    natural_syntax_ls = {
        autostart = false,
        init_options = {
            token_map_update = {
                CC = vim.NIL,
                DT = vim.NIL,
                IN = vim.NIL,
                PDT = vim.NIL,
                TO = vim.NIL,
            },
        },
    },
    pylsp = {
        settings = {
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
    },
    ruff = {
        on_attach = function(client, bufnr_attached)
            _ = client
            -- Ruff automatic import organization.
            LazyVim.format.register({
                name = "ruff.organize_imports",
                priority = 50,   -- Smaller than Conform's 100.
                primary = false, -- Conform is primary.
                format = function(bufnr)
                    if bufnr == bufnr_attached then
                        vim.lsp.buf.code_action({
                            context = {
                                only = { 'source.organizeImports' },
                                diagnostics = {},
                            },
                            apply = true,
                        })
                    end
                end,
                sources = function(_)
                    return { 'ruff.organize_imports' } -- Dummy name.
                end,
            })
        end,
    },
    solargraph = {},
    sourcekit = {
        mason = false,
    },
    svelte = {},
    tailwindcss = {},
    taplo = {},
    tsserver = {},
    zls = {},
}

local function register_mdbook_ls()
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

    require('lspconfig.configs').mdbook_ls = {
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
end

local function register_natural_syntax_ls()
    require('lspconfig.configs').natural_syntax_ls = {
        default_config = {
            cmd = {
                U.fn.expand('~/.config/helper.sh/natural-syntax-ls.sh'),
            },
            filetypes = { 'tex', 'markdown', 'text' },
            single_file_support = true,
        },
        docs = {
            description = [[The Natural Syntax Language Server for highlighting parts of speech.]],
        },
    }
end

local markdownlint_cli2_args = {
    '--config',
    U.expand('~/.config/.markdownlint-cli2.jsonc'),
}

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
                    prepend_args = markdownlint_cli2_args,
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
        opts = function(_, opts)
            register_mdbook_ls()
            register_natural_syntax_ls()
            opts.diagnostics = U.deep_extend(opts.diagnostics, {
                virtual_text = {
                    spacing = 1,
                    source = false,
                },
            })
            opts.servers = U.deep_extend(opts.servers, servers)
            opts.setup = U.deep_extend(opts.setup, {
                rust_analyzer = function() -- Prevent double setup.
                    return true
                end,
                pyright = function() -- Disable Pyright.
                    return true
                end,
            })
            for _, conf in pairs(require('lspconfig.configs')) do
                -- Disable LSP on large buffer.
                if conf.manager ~= nil and conf.manager.try_add ~= nil then
                    local try_add = conf.manager.try_add
                    conf.manager.try_add = function(bufnr)
                        if not U.b.large_buf then
                            return try_add(bufnr)
                        end
                    end
                end
            end
        end,
    },

    {
        'williamboman/mason.nvim',
        opts = function(_, opts)
            -- Override and not to install with Mason.
            local to_remove = {
                stylua = true,
            }
            for index, program in ipairs(opts.ensure_installed) do
                if to_remove[program] then
                    table.remove(opts.ensure_installed, index)
                end
            end
            -- Install these in addition.
            vim.list_extend(opts, {
                'bibtex-tidy',
                'prettierd',
            })
        end,
    },

    {
        'mfussenegger/nvim-lint',
        opts = function(_, _)
            local markdownlint_cli2 = require('lint').linters['markdownlint-cli2']
            markdownlint_cli2.args = markdownlint_cli2_args
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
