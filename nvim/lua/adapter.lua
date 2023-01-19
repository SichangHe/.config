return function(use)
    use {
        "jayp0521/mason-nvim-dap.nvim",
        requires = {
            "williamboman/mason.nvim",
            "mfussenegger/nvim-dap",
        },
        config = function()
            require("mason-nvim-dap").setup({
                automatic_setup = true,
                ensure_installed = { 'codelldb', 'python', 'node2' },
            })
        end
    }
end