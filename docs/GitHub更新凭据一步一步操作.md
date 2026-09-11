# GitHub 更新凭据：第一次设置到日常更新

这份说明适用于仓库 `fujinghui110110-cyber/hotel-budget-platform`。建议在运行预算系统的 Windows 电脑上操作，使用 Edge 或 Chrome，同时开着预算系统和 GitHub 两个标签页。

## 先分清两个账号

- **预算系统管理员账号**：用来登录 `http://127.0.0.1:8768`，管理预算。每台独立安装的服务器有自己的数据库，旧电脑的账号、密码和预算不会因为下载代码而复制过去。
- **GitHub 账号**：用来获得更新文件。这里需要能访问上述私有仓库的账号；通常就是 `fujinghui110110-cyber`。
- **下载凭据（token）**：由 GitHub 生成的一长串字符，只用于下载更新。它不是上述任意一个账号的登录密码，不要拿它登录预算系统。

只在服务器电脑配置下载凭据。各项目通过浏览器访问系统，不需要 GitHub 账号或 token。如果只用一台电脑当服务器，只需更新这一台。

## 第一步：进入 GitHub 创建页面

1. 在浏览器打开 [GitHub 官网](https://github.com)，登录 `fujinghui110110-cyber`。GitHub 如果要求邮箱验证码或双重验证，请按你自己的账号验证方式完成。
2. 打开 [本系统凭据创建链接](https://github.com/settings/personal-access-tokens/new?name=Budget-server-update&target_name=fujinghui110110-cyber&expires_in=90&contents=read)。这个链接会预填名称、仓库拥有者、90 天有效期和只读权限，**还需要你手动选择具体仓库**。
3. 如果链接没有进入创建页，也可以手动进入：右上角头像 → **Settings（设置）** → 左侧最下方 **Developer settings（开发者设置）** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**。

注意使用个人账号 Settings，不是仓库页面内的 Settings；不要选 Tokens (classic)。GitHub 可能要求再次输入 GitHub 密码，这是正常的身份确认。

## 第二步：按表填写，不需要懂编程

| 页面英文 | 应当填写或选择 |
| --- | --- |
| Token name | `Budget-server-update`，或自己能认出的名称，例如“办公室预算服务器更新” |
| Expiration | 建议 90 天。想延长可在页面允许时选自定义日期，例如 180 天；记下到期日 |
| Description | 可不填，或填“预算系统只读下载更新” |
| Resource owner | `fujinghui110110-cyber` |
| Repository access | **Only select repositories**（仅选择指定仓库） |
| Selected repositories | 搜索并勾选 **hotel-budget-platform** |
| Permissions → Repository permissions → Contents | **Read-only**（只读） |
| Metadata | 保留默认 **Read-only** |

有些 GitHub 页面会先显示 **Add permissions** 按钮；点击后选择 Repository permissions，搜索 Contents，添加后将访问级别设为 Read-only。其他权限不需要添加，不要选 Read and write，也不必授权所有仓库。

页面布局或翻译可能变化，但字段含义相同。若 Resource owner 或仓库找不到，先确认登录的是正确 GitHub 账号，并能打开 [仓库主页](https://github.com/fujinghui110110-cyber/hotel-budget-platform)。

## 第三步：生成并保存到预算系统

1. 检查仓库只选了一项、Contents 是只读，然后点击页面底部 **Generate token**。
2. GitHub 会显示一长串字符，通常以 `github_pat_` 开头。点击旁边复制按钮，复制**完整内容**。完整凭据生成后只显示一次，离开页面后不能再查看原文。
3. 回到服务器电脑的浏览器，打开 [预算系统](http://127.0.0.1:8768)，使用这台电脑的预算管理员账号登录。
4. 进入 **系统更新**，找到 **GitHub 下载凭据**，点击输入框，按 `Ctrl+V` 粘贴，然后点击 **保存凭据**。
5. 保存成功后输入框清空是正常现象，不代表没有保存。点击页面上方 **检查更新**。
6. 如果显示“已是最新版本”，也说明检查成功，不需要强行更新。如果显示有新版本，先读更新说明，然后按下一节操作。

不要把 token 发送给我、发到微信群、写进代码或放进截图。需要保存原文时可放在你自己的密码管理器中；如果遗失，重新生成一枚并替换即可。

## 以后怎样更新

1. 我们在开发电脑修改并验证代码，将代码推送到 GitHub，然后**发布稳定版本（Release）并上传系统更新包**。只有推送代码，没有发布稳定版本，服务器不会出现新版本。
2. 你在服务器电脑登录预算系统，进入“系统更新”，点击“检查更新”。
3. 有新版时，选一个项目不集中上传的时间，点击“更新并重启”。更新期间不要关闭电脑或移动系统文件夹。
4. 系统会备份数据库和配置、升级并重启。页面重新连上后，查看版本和结果；检查失败会尝试恢复旧版，具体以页面结果为准。
5. 更新完成后进入“公网访问”，重新生成公网链接，将当前链接提供给项目使用。项目不用重装软件。

这个只读 token 不用于发布代码，也不能修改仓库。后续需要改系统时，你告诉我修改要求；维护者完成测试和发布后，你再到服务器检查更新。

## 常见问题

| 现象 | 怎样处理 |
| --- | --- |
| 预算系统提示用户名或密码不正确 | 这与 GitHub token 无关。确认使用当前服务器的预算管理员账号；全新安装不会带上另一台电脑的账号。按照安装程序的管理员初始化/恢复流程处理，不要通过删除数据库解决 |
| 找不到 Developer settings | 到 GitHub 右上角头像里的个人 Settings，向左侧底部滚动；不要进入仓库 Settings |
| 手机上找不到侧栏或按钮 | 推荐改用服务器电脑浏览器，便于在两个标签页之间复制；手机可尝试浏览器“桌面版网站”，但本机地址在手机上不会指向 Windows 电脑 |
| 复制后粘贴不出来 | 再点 GitHub 的复制按钮，回系统输入框按 Ctrl+V；不要手抄长字符串，不要在聊天里中转 |
| 已关闭生成页面，没有复制 | 到 Fine-grained tokens 页面删除这枚未使用的凭据，重新生成一枚并立即保存 |
| 提示凭据无效或没有权限 | 检查是否复制完整、是否过期，是否选对拥有者与仓库，Contents 是否为 Read-only。不要改成全部权限来解决 |
| 仓库页面显示 404 或找不到仓库 | 私有仓库在没有访问权限时也可能显示 404。先确认 GitHub 登录账号正确，不要新建同名仓库 |
| 检查更新提示网络错误或超时 | 确认服务器电脑可以访问 GitHub，稍后重试；这不一定是 token 错误，本机预算功能通常仍可使用 |
| 保存后输入框为空 | 正常，系统不会重新显示凭据。看配置状态并点击“检查更新”验证 |
| 显示已是最新，但开发电脑改过代码 | 确认维护者已发布稳定 Release 和更新包，单纯推送代码不会成为可安装版本 |

## 凭据到期怎样换

到期只影响下载更新，不会清空预算或账号。重新按以上步骤生成一枚 token，在系统更新页粘贴并“保存凭据”，再“检查更新”。新凭据检查成功后，到 [Fine-grained tokens 列表](https://github.com/settings/personal-access-tokens) 删除旧凭据。若旧凭据被其他服务器共用，先为那些服务器更换，再删除；推荐每台服务器单独创建并按电脑名称命名，便于管理。

## 官方参考

步骤及预填参数已依据 GitHub 官方说明核对：

- [管理个人访问令牌（GitHub 官方）](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [创建 Fine-grained token](https://github.com/settings/personal-access-tokens/new)
