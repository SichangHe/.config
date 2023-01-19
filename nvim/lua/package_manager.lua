U = require('util')
local cmd = U.cmd
local fn = U.fn
-- Install packer
local install_path = fn.stdpath('data') .. '/site/pack/packer/start/packer.nvim'
local is_bootstrap = fn.empty(U.fn.glob(install_path)) == 1
if is_bootstrap then
    print('Bootstrapping.')
    fn.execute('!git clone https://github.com/wbthomason/packer.nvim ' .. install_path)
    cmd [[packadd packer.nvim]]
end

local packer = require('packer')

if is_bootstrap then
    packer.sync()
    print('Please wait and then restart.')
end

return packer
