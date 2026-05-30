from overleaf_sjtu.parsers import extract_csrf, extract_jaccount_login_context, page_requires_captcha, parse_project_id, parse_project_list


def test_extract_csrf_from_meta() -> None:
    html = '<html><head><meta name="ol-csrfToken" content="token123"></head></html>'
    assert extract_csrf(html) == "token123"


def test_parse_project_id_from_url() -> None:
    assert parse_project_id("https://latex.sjtu.edu.cn/project/0123456789abcdefabcdefab") == "0123456789abcdefabcdefab"


def test_parse_project_list_from_links() -> None:
    html = """
    <a href="/project/0123456789abcdefabcdefab">Paper A</a>
    <a href="/project/fedcba987654321001234567">Paper B</a>
    """
    projects = parse_project_list(html)
    assert [p.id for p in projects] == ["0123456789abcdefabcdefab", "fedcba987654321001234567"]
    assert [p.name for p in projects] == ["Paper A", "Paper B"]


def test_parse_project_list_from_prefetched_projects_meta() -> None:
    html = """
    <meta name="ol-prefetchedProjectsBlob" data-type="json"
      content="{&quot;totalSize&quot;:1,&quot;projects&quot;:[{&quot;id&quot;:&quot;0123456789abcdefabcdefab&quot;,&quot;name&quot;:&quot;Paper A&quot;,&quot;lastUpdated&quot;:&quot;2026-05-29T00:00:00.000Z&quot;,&quot;owner&quot;:{&quot;email&quot;:&quot;u@example.com&quot;}}]}">
    """
    projects = parse_project_list(html)
    assert len(projects) == 1
    assert projects[0].id == "0123456789abcdefabcdefab"
    assert projects[0].name == "Paper A"
    assert projects[0].last_updated == "2026-05-29T00:00:00.000Z"


def test_extract_jaccount_login_context() -> None:
    html = """
    <script>
    var loginContext = {
      loginType: "password",
      sid: "jaoauth220160718",
      client: "abc",
      returl:"ret",
      se: "secret",
      v: "",
      uuid: "uuid-1"
    };
    setCaptchaCheckStatus('failed');
    </script>
    """
    context = extract_jaccount_login_context(html)
    assert context["sid"] == "jaoauth220160718"
    assert context["client"] == "abc"
    assert context["uuid"] == "uuid-1"
    assert page_requires_captcha(html)
