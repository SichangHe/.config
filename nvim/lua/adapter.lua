return {
    {
        'jayp0521/mason-nvim-dap.nvim',
        event = 'VeryLazy',
        dependencies = {
            'williamboman/mason.nvim',
            'mfussenegger/nvim-dap',
        },
        opts = {
            automatic_setup = true,
            ensure_installed = { 'lldb-vscode', 'python' },
        },
    },

    {
        'mfussenegger/nvim-dap',
        event = 'VeryLazy',
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
    },

    {
        'rcarriga/nvim-dap-ui',
        event = 'VeryLazy',
        dependencies = { 'mfussenegger/nvim-dap' },
        config = true,
    },
}
