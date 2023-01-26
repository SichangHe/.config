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
        'rcarriga/nvim-dap-ui',
        event = 'VimEnter',
        requires = { 'mfussenegger/nvim-dap' },
        config = function()
            require('dapui').setup {}
        end,
    }
end