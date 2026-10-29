from __future__ import annotations

from typing import Any
import base64
from http.cookies import SimpleCookie
import json
import time
import uuid
from urllib.parse import parse_qs, urljoin, urlsplit
import httpx
from .logger import logger


class QQMusicLoginError(RuntimeError):
    """A login-stage error whose message is safe to display and log."""


class QQMusicClient:
    def __init__(self) -> None:
        self._base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            "Content-Type": "application/json;charset=utf-8",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Referer": "https://y.qq.com/",
        }
        self._cookie = ""
        self._uin = ""

    def set_cookie(self, cookie: str | dict[str, str]) -> None:
        """设置 QQ 音乐 Cookie"""
        if isinstance(cookie, dict):
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookie.items()])
        else:
            cookie_str = cookie
        
        self._cookie = cookie_str

        jar = SimpleCookie()
        try:
            jar.load(cookie_str)
        except Exception:
            jar = SimpleCookie()

        self._uin = ""
        uin_val = ""
        for k in ("qqmusic_uin", "qm_str_musicid", "musicid", "uin", "p_uin", "wxuin"):
            if k in jar and jar[k].value:
                uin_val = jar[k].value.strip()
                break

        if uin_val.startswith("o") and uin_val[1:].isdigit():
            uin_val = uin_val[1:]

        if uin_val.isdigit():
            self._uin = uin_val

    def get_cookie(self) -> str:
        """获取当前 Cookie"""
        return self._cookie

    def get_uin(self) -> str:
        """获取当前用户 UIN"""
        return self._uin

    async def _post(self, url: str, body: str, headers: dict[str, str] | None = None, use_cookie: bool = True) -> dict[str, Any]:
        """通用 POST 请求方法"""
        request_headers = self._base_headers.copy()
        if headers:
            request_headers.update(headers)
        
        # 添加 Cookie
        if use_cookie and self._cookie:
            request_headers["Cookie"] = self._cookie
        
        async with httpx.AsyncClient(timeout=20, headers=request_headers) as client:
            r = await client.post(url, content=body)
            r.raise_for_status()
            return r.json()

    async def _get(self, url: str, headers: dict[str, str] | None = None, use_cookie: bool = True) -> dict[str, Any]:
        """通用 GET 请求方法"""
        request_headers = self._base_headers.copy()
        if headers:
            request_headers.update(headers)
        
        # 添加 Cookie
        if use_cookie and self._cookie:
            request_headers["Cookie"] = self._cookie
        
        async with httpx.AsyncClient(timeout=20, headers=request_headers) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()

    def _music_key(self) -> str:
        jar = SimpleCookie()
        try:
            jar.load(self._cookie)
        except Exception:
            return ""
        return next((jar[name].value for name in ("qm_keyst", "qqmusic_key", "musickey") if name in jar and jar[name].value), "")

    async def get_music_url(self, songmid: str, quality: str = "320") -> dict[str, Any]:
        """
        获取音乐 URL
        
        Args:
            songmid: 歌曲 MID（字符串）
            quality: 歌曲品质（字符串），有 m4a、128、320（默认）可选
        """
        prefix = "C400" if quality.lower() == "m4a" else ("M500" if quality == "128" else "M800")
        suffix = "m4a" if quality.lower() == "m4a" else "mp3"
        
        # 使用真实的 uin，如果没有则使用 "0"
        uin = self._uin or "0"
        g_tk = self._get_gtk()
        music_key = self._music_key()
        detail = await self._post("https://u.y.qq.com/cgi-bin/musicu.fcg", json.dumps({
            "comm": {"ct": 24, "cv": 4747474},
            "detail": {"module": "music.pf_song_detail_svr", "method": "get_song_detail_yqq", "param": {"song_mid": songmid}},
        }))
        track = detail.get("detail", {}).get("data", {}).get("track_info", {})
        media_mid = track.get("file", {}).get("media_mid") or songmid
        
        body = {
            "req_1": {
                "module": "music.vkey.GetVkey",
                "method": "UrlGetVkey",
                "param": {
                    "filename": [f"{prefix}{media_mid}.{suffix}"],
                    "guid": "10000",
                    "songmid": [songmid],
                    "songtype": [0],
                    "uin": uin,
                    "loginflag": 1,
                    "platform": "20"
                }
            },
            "loginUin": uin,
            "comm": {
                "uin": uin,
                "format": "json",
                "ct": 24,
                "cv": 4747474,
                "platform": "yqq.json",
                "g_tk": g_tk,
                "g_tk_new_20200303": g_tk,
                "authst": music_key,
                "tmeLoginType": 1 if music_key.startswith("W_X") else 2,
            }
        }
        
        
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "content-type": "application/json;charset=UTF-8",
            "priority": "u=1, i",
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "none",
            "sec-fetch-storage-access": "active",
        }
        
        return await self._post(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            json.dumps(body),
            headers
        )

    async def get_song_list(self, category_id: str) -> dict[str, Any]:
        """
        获取歌单歌曲信息
        
        Args:
            category_id: 歌单 ID
        """
        url = f"https://i.y.qq.com/qzone-music/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?type=1&json=1&utf8=1&onlysong=0&nosign=1&disstid={category_id}&g_tk=5381&loginUin=0&hostUin=0&format=json&inCharset=GB2312&outCharset=utf-8&notice=0&platform=yqq&needNewCode=0"
        return await self._get(url)

    async def get_song_list_name(self, category_id: str) -> dict[str, Any]:
        """
        获取歌单名称
        
        Args:
            category_id: 歌单 ID
        """
        return await self.get_song_list(category_id)

    async def search_with_keyword(
        self,
        keyword: str,
        search_type: int = 0,
        result_num: int = 50,
        page_num: int = 1
    ) -> dict[str, Any]:
        """
        用关键词搜索歌曲
        
        Args:
            keyword: 关键词（字符串）
            search_type: 搜索结果类型（默认为 0），0 为歌曲，2 为专辑，3 为歌单，4 为 MV，7 为歌词，8 为用户
            result_num: （每页）结果数量（默认为 50）
            page_num: 页面序号（不是页数，默认为 1）
        """
        body = {
            "comm": {
                "ct": "19",
                "cv": "1859",
                "uin": "0"
            },
            "req": {
                "method": "DoSearchForQQMusicDesktop",
                "module": "music.search.SearchCgiService",
                "param": {
                    "grp": 1,
                    "num_per_page": result_num,
                    "page_num": page_num,
                    "query": keyword,
                    "search_type": search_type
                }
            }
        }
        
        return await self._post(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            json.dumps(body)
        )

    async def get_song_lyric(self, songmid: str) -> dict[str, Any]:
        """
        获取歌曲歌词
        
        Args:
            songmid: 歌曲 MID（字符串）
        """
        url = f"https://i.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={songmid}&g_tk=5381&format=json&inCharset=utf8&outCharset=utf-8&nobase64=1"
        return await self._get(url)

    async def get_album_song_list(self, albummid: str) -> dict[str, Any]:
        """
        获取专辑歌曲信息
        
        Args:
            albummid: 专辑的 MID
        """
        url = f"https://i.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg?platform=h5page&albummid={albummid}&g_tk=938407465&uin=0&format=json&inCharset=utf-8&outCharset=utf-8&notice=0&platform=h5&needNewCode=1&_=1459961045571"
        return await self._get(url)

    async def get_album_name(self, albummid: str) -> dict[str, Any]:
        """
        获取专辑名称
        
        Args:
            albummid: 专辑的 MID
        """
        return await self.get_album_song_list(albummid)

    async def get_mv_info(self, vid: str) -> dict[str, Any]:
        """
        获取 MV 信息
        
        Args:
            vid: MV 的 VID
        """
        body = {
            "comm": {
                "ct": 6,
                "cv": 0,
                "g_tk": 1646675364,
                "uin": 0,
                "format": "json",
                "platform": "yqq"
            },
            "mvInfo": {
                "module": "music.video.VideoData",
                "method": "get_video_info_batch",
                "param": {
                    "vidlist": [vid],
                    "required": [
                        "vid", "type", "sid", "cover_pic", "duration", "singers",
                        "new_switch_str", "video_pay", "hint", "code", "msg", "name",
                        "desc", "playcnt", "pubdate", "isfav", "fileid", "filesize_v2",
                        "switch_pay_type", "pay", "pay_info", "uploader_headurl",
                        "uploader_nick", "uploader_uin", "uploader_encuin", "play_forbid_reason"
                    ]
                }
            },
            "mvUrl": {
                "module": "music.stream.MvUrlProxy",
                "method": "GetMvUrls",
                "param": {
                    "vids": [vid],
                    "request_type": 10003,
                    "addrtype": 3,
                    "format": 264,
                    "maxFiletype": 60
                }
            }
        }
        
        headers = {
            "Content-type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "same-site",
        }
        headers.update(self._base_headers)
        
        return await self._post(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            json.dumps(body),
            headers
        )

    async def get_singer_info(self, singermid: str) -> dict[str, Any]:
        """
        获取歌手信息
        
        Args:
            singermid: 歌手 MID
        """
        url = f"https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&loginUin=0&hostUin=0inCharset=utf8&outCharset=utf-8&platform=yqq.json&needNewCode=0&data=%7B%22comm%22%3A%7B%22ct%22%3A24%2C%22cv%22%3A0%7D%2C%22singer%22%3A%7B%22method%22%3A%22get_singer_detail_info%22%2C%22param%22%3A%7B%22sort%22%3A5%2C%22singermid%22%3A%22{singermid}%22%2C%22sin%22%3A0%2C%22num%22%3A50%7D%2C%22module%22%3A%22music.web_singer_info_svr%22%7D%7D"
        return await self._get(url)

    # 工具函数
    def get_album_cover_image(self, albummid: str) -> str:
        """
        获取专辑封面图
        
        Args:
            albummid: 专辑的 MID
        """
        return f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg"
    
    def get_song_cover_image(self, album_mid: str) -> str:
        """
        获取歌曲封面图（通过专辑 MID）
        
        Args:
            album_mid: 专辑的 MID
        """
        # QQ 音乐封面 URL 格式，使用专辑 MID
        if not album_mid:
            return ""
        return f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"

    def parse_lyric(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        解析歌词
        
        Args:
            data: 从 QQ 音乐申请来的数据
        """
        parsed = {
            "ti": "",
            "ar": "",
            "al": "",
            "by": "",
            "offset": "",
            "count": 0,
            "haveTrans": False,
            "lyric": [],
        }
        
        lyric_lines = data.get("lyric", "").split("\n")
        trans_lines = data.get("trans", "").split("\n")
        parsed["haveTrans"] = bool(data.get("trans", "").strip())

        def substr(s: str) -> str:
            start = s.find(":") + 1
            end = s.find("]")
            return s[start:end] if start > 0 and end > start else ""

        if lyric_lines and not lyric_lines[0].startswith("[0"):
            parsed["ti"] = substr(lyric_lines[0]) if len(lyric_lines) > 0 else ""
            parsed["ar"] = substr(lyric_lines[1]) if len(lyric_lines) > 1 else ""
            parsed["al"] = substr(lyric_lines[2]) if len(lyric_lines) > 2 else ""
            parsed["by"] = substr(lyric_lines[3]) if len(lyric_lines) > 3 else ""
            parsed["offset"] = substr(lyric_lines[4]) if len(lyric_lines) > 4 else ""
            lyric_lines = lyric_lines[5:]
            if parsed["haveTrans"]:
                trans_lines = trans_lines[5:]

        parsed["count"] = len(lyric_lines)
        for i in range(parsed["count"]):
            if i >= len(lyric_lines):
                break
                
            line = lyric_lines[i]
            ele = {"time": "", "lyric": "", "trans": ""}
            
            # 提取时间
            end_bracket = line.find("]")
            if end_bracket > 0:
                ele["time"] = line[1:end_bracket]
                ele["lyric"] = line[end_bracket + 1:]
            else:
                ele["lyric"] = line
            
            # 提取翻译
            if parsed["haveTrans"] and i < len(trans_lines):
                trans_line = trans_lines[i]
                trans_end_bracket = trans_line.find("]")
                if trans_end_bracket > 0:
                    ele["trans"] = trans_line[trans_end_bracket + 1:]
                else:
                    ele["trans"] = trans_line
            
            parsed["lyric"].append(ele)

        return parsed

    # 简化的 API 方法，返回处理后的数据
    async def get_music_url_simple(self, songmid: str, quality: str = "320") -> str | None:
        """获取音乐播放 URL（简化版）"""
        try:
            data = await self.get_music_url(songmid, quality)
            
            req_data = data.get("req_1", {}).get("data", {})
            sip = req_data.get("sip", [])
            midurlinfo = req_data.get("midurlinfo", [])
            
            
            if sip and midurlinfo:
                midurl_item = midurlinfo[0]
                
                # 优先使用完整播放链接
                if midurl_item.get("purl"):
                    url = urljoin(sip[0], midurl_item["purl"])
                    return url
                
                # 如果没有完整链接，尝试试听链接
                if midurl_item.get("opi30surl"):
                    logger.info("QQ 音乐播放：仅取得试听地址")
                    return midurl_item["opi30surl"]
                
                # 检查其他可用的链接
                for key in ["opi48kurl", "opi96kurl", "opi128kurl", "opi192kurl"]:
                    if midurl_item.get(key):
                        return midurl_item[key]
                
                result_code = midurl_item.get('result')
                logger.info("QQ 音乐播放：未取得地址，结果码=%s", result_code)
                
                # 根据错误码提供更具体的错误信息
                if result_code == 104003:
                    raise Exception("该歌曲需要 VIP 会员或付费购买才能播放")
                elif result_code:
                    raise Exception(f"无法获取播放链接，错误码: {result_code}")
            
            return None
        except Exception as e:
            logger.warning("QQ 音乐播放：请求失败，异常类型=%s", type(e).__name__)
            return None

    async def verify_playback(self) -> dict[str, Any]:
        """Verify one Jay Chou track without adding it to the queue or downloading the song."""
        checked_at = int(time.time())
        song_name = ""
        stage = "credentials"
        try:
            if not self._uin or not self._music_key():
                return {"status": "failed", "message": "未取得完整 QQ 音乐账号和音乐密钥，请重新扫码授权", "checked_at": checked_at}
            stage = "search"
            search = await self.search_with_keyword("周杰伦 晴天", result_num=10)
            songs = search.get("req", {}).get("data", {}).get("body", {}).get("song", {}).get("list", [])
            songs = [song for song in songs if song.get("mid") and any(singer.get("name") == "周杰伦" for singer in song.get("singer", []))]
            if not songs:
                raise QQMusicLoginError("未搜索到周杰伦歌曲，无法完成点播验证")
            song = next((song for song in songs if song.get("pay", {}).get("pay_play") == 1), songs[0])
            song_name = str(song.get("name") or "测试歌曲")
            stage = "vkey"
            raw = await self.get_music_url(song["mid"], "320")
            module = raw.get("req_1", {})
            data = module.get("data", {})
            items = data.get("midurlinfo", [])
            item = items[0] if items else {}
            code = item.get("result", module.get("code", raw.get("code")))
            logger.info("QQ 音乐点播验证：标准地址=%s，结果码=%s", bool(item.get("purl")), code)
            if raw.get("code", 0) != 0 or module.get("code", 0) != 0 or item.get("result", 0) not in (0, "0") or not item.get("purl"):
                return {"status": "failed", "song": song_name, "checked_at": checked_at,
                        "message": f"周杰伦《{song_name}》未取得标准播放地址；可能涉及登录、会员权限、版权或地区限制，试听不算通过"}
            url = urljoin((data.get("sip") or [""])[0], item["purl"])
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "http"} or not (parsed.hostname or "").endswith(".qq.com"):
                raise QQMusicLoginError("播放接口未返回可验证的 QQ 音乐 CDN 地址")
            url = parsed._replace(scheme="https").geturl()
            stage = "cdn"
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                async with client.stream("GET", url, headers={"Range": "bytes=0-1023", "Referer": "https://y.qq.com/"}) as response:
                    logger.info("QQ 音乐点播验证：CDN HTTP %s", response.status_code)
                    if response.status_code not in (200, 206):
                        raise QQMusicLoginError(f"已取得播放地址，但音频读取失败（HTTP {response.status_code}）")
                    sample = b""
                    async for chunk in response.aiter_bytes():
                        sample += chunk[:1024 - len(sample)]
                        if len(sample) >= 32:
                            break
                    if not sample or not (sample.startswith((b"ID3", b"fLaC")) or (len(sample) >= 2 and sample[0] == 255 and sample[1] & 224 == 224) or b"ftyp" in sample[:32]):
                        raise QQMusicLoginError("播放地址返回的内容未识别为音频，验证未通过")
            logger.info("QQ 音乐点播验证：通过，已读取标准播放地址的音频片段")
            return {"status": "passed", "song": song_name, "checked_at": checked_at,
                    "message": f"周杰伦《{song_name}》标准播放地址可读取，点播授权验证通过"}
        except QQMusicLoginError as exc:
            logger.warning("QQ 音乐点播验证：阶段=%s，%s", stage, exc)
            return {"status": "failed", "song": song_name, "checked_at": checked_at, "message": str(exc)}
        except Exception as exc:
            logger.warning("QQ 音乐点播验证：阶段=%s，异常类型=%s", stage, type(exc).__name__)
            return {"status": "error", "song": song_name, "checked_at": checked_at,
                    "message": "点播验证请求失败，请检查服务器网络并查看验证日志；暂时无法判断登录有效性"}

    async def get_song_list_simple(self, category_id: str) -> list[dict[str, Any]]:
        """获取歌单歌曲信息（简化版）"""
        try:
            data = await self.get_song_list(category_id)
            cdlist = data.get("cdlist", [])
            if cdlist:
                return cdlist[0].get("songlist", [])
            return []
        except Exception:
            return []

    async def get_song_list_name_simple(self, category_id: str) -> str | None:
        """获取歌单名称（简化版）"""
        try:
            data = await self.get_song_list(category_id)
            cdlist = data.get("cdlist", [])
            if cdlist:
                return cdlist[0].get("dissname")
            return None
        except Exception:
            return None

    async def search_songs_simple(self, keyword: str, limit: int = 50, page: int = 1) -> list[dict[str, Any]]:
        """搜索歌曲（简化版）"""
        try:
            data = await self.search_with_keyword(keyword, search_type=0, result_num=limit, page_num=page)
            return data.get("req", {}).get("data", {}).get("body", {}).get("song", {}).get("list", [])
        except Exception:
            return []

    async def get_song_lyric_simple(self, songmid: str, parse: bool = False) -> str | dict[str, Any] | None:
        """获取歌曲歌词（简化版）"""
        try:
            data = await self.get_song_lyric(songmid)
            if parse:
                return self.parse_lyric(data)
            else:
                lyric = data.get("lyric", "")
                trans = data.get("trans", "")
                return lyric + "\n" + trans if trans else lyric
        except Exception:
            return None

    # QQ Music Login functionality
    async def get_qr_key(self) -> dict[str, Any]:
        """获取二维码登录密钥"""
        import random
        import time
        import base64
        
        # First, initialize login session to get pt_login_sig
        try:
            logger.info("QQ 音乐扫码：开始创建登录会话")
            xlogin_url = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
            xlogin_params = {
                "appid": "716027609",
                "daid": "383",
                "style": "33",
                "login_text": "授权并登录",
                "hide_title_bar": "1",
                "hide_border": "1",
                "target": "self",
                "s_url": "https://graph.qq.com/oauth2.0/login_jump",
                "pt_3rd_aid": "100497308",
                "pt_feedback_link": "https://support.qq.com/products/77942?customInfo=.appid100497308",
            }
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "https://xui.ptlogin2.qq.com/"
            }
            
            async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
                # Initialize session
                xlogin_response = await client.get(xlogin_url, params=xlogin_params)
                logger.info("QQ 音乐扫码：初始化响应 HTTP %s", xlogin_response.status_code)
                
                # Get pt_login_sig from cookies
                pt_login_sig = None
                if xlogin_response.cookies:
                    pt_login_sig = xlogin_response.cookies.get("pt_login_sig")
                
                if not pt_login_sig:
                    raise Exception("Failed to get pt_login_sig from xlogin")
                
                # Store session cookies and pt_login_sig
                self._session_cookies = {}
                for name, value in xlogin_response.cookies.items():
                    self._session_cookies[name] = value
                
                self._pt_login_sig = pt_login_sig
                self._last_qr_status = None
                
                # Now get QR code
                timestamp = str(int(time.time() * 1000))
                random_suffix = str(random.randint(100, 999))
                qr_token = timestamp + random_suffix
                
                qr_url = "https://ssl.ptlogin2.qq.com/ptqrshow"
                qr_params = {
                    "appid": "716027609",
                    "e": "2",
                    "l": "M",
                    "s": "3",
                    "d": "72",
                    "v": "4",
                    "t": qr_token,
                    "daid": "383",
                    "pt_3rd_aid": "100497308"
                }
                
                qr_response = await client.get(qr_url, params=qr_params)
                logger.info("QQ 音乐扫码：二维码响应 HTTP %s", qr_response.status_code)
                
                qr_response.raise_for_status()
                
                # Get qrsig from cookies
                qrsig = None
                if qr_response.cookies:
                    qrsig = qr_response.cookies.get("qrsig")
                    
                    # Add qrsig to session cookies
                    self._session_cookies["qrsig"] = qrsig
                
                if not qrsig:
                    raise Exception("Failed to get qrsig from QR response")
                
                # Build QR URL
                param_str = "&".join([f"{k}={v}" for k, v in qr_params.items()])
                qr_url_full = f"{qr_url}?{param_str}"

                qr_image_base64 = base64.b64encode(qr_response.content).decode("ascii")
                
                # Calculate ptqrtoken using qrsig
                ptqrtoken = self._hash33(qrsig)
                
                logger.info("QQ 音乐扫码：二维码已生成，等待扫码")
                
                return {
                    "qr_url": qr_url_full,
                    "qr_image_base64": qr_image_base64,
                    "qr_key": qrsig,
                    "ptqrtoken": ptqrtoken,
                    "pt_login_sig": pt_login_sig,
                    "debug_info": {
                        "original_token": qr_token,
                        "cookies_found": len(qr_response.cookies) if qr_response.cookies else 0,
                        "status_code": qr_response.status_code
                    }
                }
        except Exception as e:
            logger.warning("QQ 音乐扫码：生成二维码失败，异常类型 %s", type(e).__name__)
            raise Exception(f"获取二维码失败（{type(e).__name__}）") from None

    def _cookie_header_from_session(self) -> str:
        parts: list[str] = []
        if hasattr(self, "_session_cookies") and isinstance(self._session_cookies, dict):
            for k, v in self._session_cookies.items():
                if v is None:
                    continue
                parts.append(f"{k}={v}")
        return "; ".join(parts)

    def _merge_response_cookies_into_session(self, cookies: httpx.Cookies) -> None:
        if not hasattr(self, "_session_cookies") or not isinstance(self._session_cookies, dict):
            self._session_cookies = {}
        for item in cookies.jar:
            self._session_cookies[item.name] = item.value

    async def confirm_qr_login(self, auth_url: str) -> dict[str, Any]:
        """将已确认的 QQ 扫码会话换成音乐凭据，完整取得后再替换登录态。"""
        parsed = urlsplit(auth_url)
        if parsed.scheme != "https" or parsed.hostname not in {"ssl.ptlogin2.graph.qq.com", "ssl.ptlogin2.qq.com"} or parsed.path != "/check_sig":
            raise QQMusicLoginError("QQ 授权回调地址无效，请重新扫码")
        query = parse_qs(parsed.query)
        uin = query.get("uin", [""])[0]
        sig = query.get("ptsigx", [""])[0]
        if not uin.isdigit() or not sig:
            raise QQMusicLoginError("QQ 授权回调缺少必要参数，请重新扫码")
        cookies = dict(getattr(self, "_session_cookies", {}))
        def cookie_header() -> str:
            return "; ".join(f"{key}={value}" for key, value in cookies.items())

        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            callback = await client.get("https://ssl.ptlogin2.graph.qq.com/check_sig", params={
                "uin": uin, "ptsigx": sig, "pttype": "1", "service": "ptqrlogin", "nodirect": "0",
                "s_url": "https://graph.qq.com/oauth2.0/login_jump", "ptlang": "2052", "ptredirect": "100",
                "aid": "716027609", "daid": "383", "pt_3rd_aid": "100497308",
                "j_later": "0", "low_login_hour": "0", "regmaster": "0", "pt_login_type": "3",
                "pt_aid": "0", "pt_aaid": "16", "pt_light": "0",
            }, headers={"User-Agent": self._base_headers["User-Agent"], "Referer": "https://xui.ptlogin2.qq.com/", "Cookie": cookie_header()})
            logger.info("QQ 音乐扫码：QQ 授权回调 HTTP %s", callback.status_code)
            if not 200 <= callback.status_code < 400:
                raise QQMusicLoginError("QQ 授权回调失败，请重新扫码")
            for item in callback.cookies.jar:
                cookies[item.name] = item.value
            if not cookies.get("p_skey"):
                raise QQMusicLoginError("QQ 授权未取得 p_skey，无法继续登录音乐服务")
            authorization = await client.post("https://graph.qq.com/oauth2.0/authorize", data={
                "response_type": "code", "client_id": "100497308",
                "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",
                "scope": "get_user_info,get_app_friends", "state": "state", "switch": "",
                "from_ptlogin": "1", "src": "1", "update_auth": "1", "openapi": "1010_1030",
                "g_tk": self._hash33(cookies["p_skey"], 5381), "auth_time": str(int(time.time() * 1000)),
                "ui": str(uuid.uuid4()).upper(),
            }, headers={"User-Agent": self._base_headers["User-Agent"], "Referer": "https://graph.qq.com/oauth2.0/login_jump", "Origin": "https://graph.qq.com", "Cookie": cookie_header()})
            code = parse_qs(urlsplit(authorization.headers.get("location", "")).query).get("code", [""])[0]
            logger.info("QQ 音乐扫码：OAuth HTTP %s，有授权码=%s", authorization.status_code, bool(code))
            if not authorization.is_redirect or not code:
                raise QQMusicLoginError("QQ OAuth 授权未返回授权码，请重新扫码")
            music = await client.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json={
                "comm": {"ct": 24, "cv": 4747474, "platform": "yqq.json", "uin": "0", "g_tk": 5381, "tmeLoginType": 2},
                "req_0": {"module": "QQConnectLogin.LoginServer", "method": "QQLogin", "param": {"code": code}},
            }, headers={"User-Agent": self._base_headers["User-Agent"], "Referer": "https://y.qq.com/", "Origin": "https://y.qq.com", "Cookie": cookie_header()})
            logger.info("QQ 音乐扫码：音乐凭据交换 HTTP %s", music.status_code)
            music.raise_for_status()
            result = music.json()
            item = result.get("req_0", {})
            data = item.get("data", {})
            logger.info("QQ 音乐扫码：音乐凭据交换结果 code=%s，业务 code=%s", item.get("code"), data.get("code"))
            if result.get("code", 0) != 0 or item.get("code") != 0 or data.get("code", 0) != 0:
                raise QQMusicLoginError("QQ 音乐拒绝凭据交换，请查看扫码日志中的结果码")
            credential = data.get("data", data)
            musicid = str(credential.get("str_musicid") or credential.get("musicid") or "")
            musickey = credential.get("musickey") or credential.get("musicKey") or credential.get("qm_keyst")
            if not musicid.isdigit() or not musickey:
                raise QQMusicLoginError("QQ 音乐返回的音乐账号或密钥不完整，未保存登录")

        # Only music credentials are persisted; QR signatures and OAuth codes stay transient.
        self.set_cookie({"uin": musicid, "qqmusic_uin": musicid, "qm_str_musicid": musicid, "musicid": musicid,
                         "qm_keyst": musickey, "qqmusic_key": musickey, "musickey": musickey, "tmeLoginType": "2"})
        logger.info("QQ 音乐扫码：已取得完整音乐凭据")
        return {"ok": True, "uin": self.get_uin()}

    def _hash33(self, t: str, seed: int = 0) -> str:
        """计算 ptqrtoken"""
        e = seed
        for i in range(len(t)):
            e += (e << 5) + ord(t[i])
        return str(e & 2147483647)

    async def check_qr_status(self, qr_key: str, ptqrtoken: str) -> dict[str, Any]:
        """检查二维码登录状态"""
        url = "https://ssl.ptlogin2.qq.com/ptqrlogin"
        params = {
            "u1": "https://graph.qq.com/oauth2.0/login_jump",
            "ptqrtoken": ptqrtoken,
            "ptredirect": "0",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "action": f"0-0-{int(time.time() * 1000)}",
            "js_ver": "20102616",
            "js_type": "1",
            "login_sig": getattr(self, '_pt_login_sig', ''),
            "pt_uistyle": "40",
            "aid": "716027609",
            "daid": "383",
            "pt_3rd_aid": "100497308",
            "has_onekey": "1",
        }

        headers = {
            "User-Agent": self._base_headers.get("User-Agent", ""),
            "Referer": "https://xui.ptlogin2.qq.com/",
        }
        cookie_header = self._cookie_header_from_session()
        headers["Cookie"] = cookie_header or f"qrsig={qr_key}"

        try:
            async with httpx.AsyncClient(timeout=10, headers=headers) as client:
                response = await client.get(url, params=params)
                self._merge_response_cookies_into_session(response.cookies)
                response_text = response.text

                import re
                match = re.search(r"ptuiCB\((.*?)\)", response_text)
                if not match:
                    logger.warning("QQ 音乐扫码：状态响应无法解析，HTTP %s", response.status_code)
                    return {"status": "error", "message": "获取二维码状态失败"}

                callback_data = match.group(1)
                data = [p.strip().strip("'").strip('"') for p in callback_data.split(",")]
                if not data:
                    return {"status": "error", "message": "解析回调数据失败"}

                code_str = data[0]
                if not code_str.isdigit():
                    return {"status": "error", "message": f"未知状态码: {code_str}"}
                code = int(code_str)
                if code != getattr(self, "_last_qr_status", None):
                    logger.info("QQ 音乐扫码：状态码 %s", code)
                    self._last_qr_status = code

                if code == 0:
                    auth_url = data[2] if len(data) > 2 else ""
                    uin_match = re.search(r"&uin=(.+?)&", auth_url)
                    sigx_match = re.search(r"&ptsigx=(.+?)&", auth_url)
                    payload: dict[str, Any] = {"status": "success", "message": "登录成功"}
                    if auth_url:
                        payload["auth_url"] = auth_url
                    if uin_match:
                        payload["uin"] = uin_match.group(1)
                    if sigx_match:
                        payload["sigx"] = sigx_match.group(1)
                    return payload
                if code == 65:
                    return {"status": "expired", "message": "二维码已失效"}
                if code == 66:
                    return {"status": "waiting", "message": "二维码未失效，请扫码"}
                if code == 67:
                    return {"status": "scanning", "message": "二维码认证中，请在手机上确认"}
                if code == 68:
                    return {"status": "expired", "message": "二维码已失效"}

                message = data[4] if len(data) > 4 and data[4] else f"状态码: {code}"
                return {"status": "waiting", "message": message}
        except Exception as e:
            logger.warning("QQ 音乐扫码：状态查询失败，异常类型 %s", type(e).__name__)
            return {"status": "error", "message": f"检查登录状态失败: {str(e)}"}

    async def refresh_login(self) -> dict[str, Any]:
        """刷新登录状态"""
        if not self._cookie:
            return {"success": False, "message": "未设置 Cookie"}
        
        try:
            # 尝试获取用户信息来验证登录状态
            user_info = await self.get_user_info()
            if user_info and user_info.get("code") == 0:
                return {"success": True, "message": "登录状态有效"}
            else:
                return {"success": False, "message": "登录状态已失效"}
        except Exception as e:
            return {"success": False, "message": f"刷新登录失败: {str(e)}"}

    async def get_user_info(self) -> dict[str, Any]:
        """获取用户信息"""
        if not self._cookie:
            raise Exception("未设置 Cookie，请先登录")
        
        url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
        body = {
            "comm": {
                "ct": 24,
                "cv": 0
            },
            "req_0": {
                "module": "music.user.UserInfoServer",
                "method": "get_user_detail_info",
                "param": {
                    "vec_uin": [self._uin] if self._uin else []
                }
            }
        }
        
        return await self._post(url, json.dumps(body))

    async def get_user_playlists(self) -> dict[str, Any]:
        """获取用户歌单"""
        if not self._cookie or not self._uin:
            raise Exception("未设置 Cookie 或 UIN，请先登录")
        
        url = "https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss"
        params = {
            "hostuin": self._uin,
            "sin": "0",
            "size": "200",
            "g_tk": self._get_gtk(),
            "loginUin": self._uin,
            "hostUin": "0",
            "format": "json",
            "inCharset": "utf8",
            "outCharset": "utf-8",
            "notice": "0",
            "platform": "yqq.json",
            "needNewCode": "0"
        }
        
        param_str = "&".join([f"{k}={v}" for k, v in params.items()])
        full_url = f"{url}?{param_str}"
        
        return await self._get(full_url)

    def _get_gtk(self) -> str:
        """计算 g_tk 参数"""
        if not self._cookie:
            return "0"

        music_key = self._music_key()
        if music_key:
            return self._hash33(music_key, 5381)

        jar = SimpleCookie()
        try:
            jar.load(self._cookie)
        except Exception:
            jar = SimpleCookie()

        p_skey = jar.get("p_skey").value.strip() if jar.get("p_skey") and jar.get("p_skey").value else ""
        if not p_skey:
            p_skey = jar.get("skey").value.strip() if jar.get("skey") and jar.get("skey").value else ""
        if not p_skey:
            return "0"
        
        # 计算 gtk
        gtk = 5381
        for char in p_skey:
            gtk += (gtk << 5) + ord(char)
        
        return str(gtk & 2147483647)
