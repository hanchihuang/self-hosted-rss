# self-hosted-rss

一个本地自建的轻量 RSS 阅读器，支持：

- 默认导入 `The Most Popular Blogs of Hacker News in 2025` 的 OPML 订阅源
- OPML 粘贴导入
- 全量刷新和文章本地缓存
- 文章搜索
- Feed 标签管理和按标签过滤
- 文章收藏
- 后台定时自动刷新

项目适合本机自用，依赖少，启动简单。

## 技术栈

- Python 3
- Flask
- SQLite
- feedparser
- BeautifulSoup

## 目录结构

```text
rss_reader_app/
├── app.py
├── requirements.txt
├── feeds/
│   └── hn-popular-blogs-2025.opml
├── templates/
│   ├── base.html
│   ├── index.html
│   └── feed_detail.html
└── rss_reader.db
```

## 功能说明

### 1. 默认订阅源

首次启动会自动初始化数据库，并导入内置 OPML：

- 文件：`feeds/hn-popular-blogs-2025.opml`
- 当前默认导入源数量：92

### 2. 搜索

首页支持按以下字段搜索：

- 文章标题
- 摘要
- 作者
- 来源订阅名

### 3. 标签

标签绑定在订阅源上，不是单篇文章标签。

适合这种用法：

- 给技术博客打 `ai`
- 给安全博客打 `security`
- 给编程博客打 `programming`

然后在首页直接按标签过滤整类文章流。

### 4. 收藏

每篇文章支持收藏/取消收藏。

收藏后可以通过：

- 首页 `只看收藏`

快速查看你标过的文章。

### 5. 自动刷新

支持后台线程定时刷新全部订阅：

- 可在页面里开启/关闭
- 可设置刷新间隔分钟数

默认配置：

- 开启自动刷新
- 每 30 分钟刷新一次

## 本地运行

```bash
cd /home/user/图片/rss_reader_app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

打开：

```text
http://127.0.0.1:5050
```

## 页面操作

启动后你可以：

1. 先点 `刷新全部订阅`
2. 看首页最新文章流
3. 给订阅源打标签
4. 搜索文章
5. 收藏想留存的文章
6. 设置后台自动刷新

## 数据存储

- 数据库：`rss_reader.db`
- 订阅源：`feeds`
- 文章缓存：`entries`
- 调度设置：`settings`

## 注意事项

- 某些 RSS 源可能失效、限流或超时，刷新时会记录错误，但不会影响其他源
- 这是本地自托管轻量版本，不包含用户系统、云同步、多用户权限等功能
- 开发服务器使用 Flask 内置 server，适合本地使用；如果要长期部署可换成 gunicorn 等 WSGI 服务

## 后续可扩展方向

- 全文抓取与正文提取
- 已读/未读状态
- 关键词高亮
- 邮件订阅导入
- 多用户支持
- Docker 化部署
