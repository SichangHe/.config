return function(use)
    use {
        'jayp0521/mason-nvim-dap.nvim',
        event = 'VimEnter',
        requires = {
            'williamboman/mason.nvim',
            'mfussenegger/nvim-dap',
        },
        config = function()
            require('mason-nvim-dap').setup({
                automatic_setup = true,
                ensure_installed = { 'lldb-vscode', 'python' },
            })
        end,
    }

    use {
        'mfussenegger/nvim-dap',
        event = 'VimEnter',
        config = function()
            local dap = require('dap')

            dap.adapters.mix_task = {
                type = 'executable',
                command = 'elixir-ls-debugger',
                args = {}
            }
            dap.configurations.elixir = {
                {
                    type = "mix_task",
                    name = "mix test",
                    task = 'test',
                    taskArgs = { "--trace" },
                    request = "launch",
                    startApps = true, -- for Phoenix projects
                    projectDir = "${workspaceFolder}",
                    requireFiles = {
                        "test/**/test_helper.exs",
                        "test/**/*_test.exs"
                    },
                },
            }
        end
    }

    use {
        'rcarriga/nvim-dap-ui',
        event = 'VimEnter',
        requires = { 'mfussenegger/nvim-dap' },
        config = function()
            require('dapui').setup {}
        end,
    }
end
