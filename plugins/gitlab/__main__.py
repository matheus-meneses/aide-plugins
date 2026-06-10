from scraper import GitlabScraper

from aide_sdk.runtime import serve

if __name__ == "__main__":
    serve(GitlabScraper)
