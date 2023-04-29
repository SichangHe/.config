-- This is an exacutable, so we abuse global variables.
package.path = debug.getinfo(1).source:match("@?(.*/)") .. '/?.lua;' .. package.path
Command = require('command')
Noti = require('notification')

YABAI = ' /opt/homebrew/bin/yabai '
JQ = ' ~/.nix-profile/bin/jq '

function get_last()
    if id == id_list[1] then
        return id_list[#id_list]
    end
    for _, now in ipairs(id_list) do
        if now == id then break end
        last = now
    end
    return last
end

function get_next()
    if id == id_list[#id_list] then
        return id_list[1]
    end
    for i, now in ipairs(id_list) do
        if now == id then return id_list[i + 1] end
    end
end

function cycle()
    ids = Command.run(YABAI .. "-m query --windows --space |" .. JQ .. "'.[].id'")
    id = tonumber(Command.run(YABAI .. "-m query --windows --window |" .. JQ .. "'.id'"))

    id_list = {}
    for now in string.gmatch(ids, "%w+") do
        table.insert(id_list, tonumber(now))
    end
    table.sort(id_list)

    target = Args[1] == 'next' and get_next() or get_last()
    return Command.run(YABAI .. "-m window --focus " .. target)
end

function info()
    apps = Command.run(YABAI .. "-m query --windows --space |" .. JQ .. "'.[].app'")
    app_str = ''
    for app in string.gmatch(apps, "\"%w+[%s\"]") do
        app_str = app_str .. string.match(app, "%w+") .. ' '
    end
    space_id = Command.run(YABAI .. "-m query --spaces --space |" .. JQ .. "'.index'", "*l")
    window_num = Command.run(YABAI .. "-m query --windows --space |" .. JQ .. "'.[].title' | wc -l", "*l")
    return Noti.display(app_str, 'Space ' .. space_id, window_num .. ' windows')
end

Args = { ... }
if Args[1] == 'next' or Args[1] == 'last' then
    result = cycle()
else
    if Args[1] == 'info' then
        result = info()
    end
end
if result ~= '' then
    Noti.display(result, 'yabai', 'script failed')
end
