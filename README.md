# nola-tax-assessor-scraper

I took a look at reviving [my tax scraper](https://github.com/bhelx/nola-assessor-data) project to see if i could get some new numbers.
But it looks like the vendor has added new restrictions (and a cloudflare WAF) to prevent people
from scraping the data. Which feels insulting considering they still want to charge $50k
for the data, which is public domain.

So I found a workaround for the time being. The city releases all of their tax rolls every year,
but in a very [weird PDF format](https://nola.gov/tax-rolls/). It's quite annoying. Obviously they
need the raw data to generate these PDFs, so why can't they just give us the raw data? It's almost
like they want to make analyzing this data as hard as possible for some reason.

I had seen this before but went around this problem because parsing PDFs is a huge pain. Flash forward
to 2026 and coding agents have gotten quite good at this kind of thing. So this new approach
pulls all the PDFs and parses them into a sqlite file. This sqlite file has a geometries and lat lngs
as well so you can do analysis on it.

The data goes from 2020 to 2026 (the PDFs before 2020 is appears to just be images...). Also note that this
has less data. We only have the properties and the tax assessment values. We don't have the sale data, etc,
like we do with the scraper. Maybe more can be amended here from some other data source.

For example, here is a query that can look at the relative change of taxes and group by neighborhood:


```sql
WITH tax_by_neighborhood_year AS (
    SELECT pc.neighborhood,
           p.tax_year,
           SUM(p.net_tax) AS total_net_tax
    FROM properties p
    JOIN parcels pc ON pc.parcel_id = p.parcel_id
    WHERE p.tax_year IN (2024, 2025)
      AND pc.neighborhood IS NOT NULL
      AND p.net_tax IS NOT NULL
    GROUP BY pc.neighborhood, p.tax_year
)
SELECT
    y2024.neighborhood,
    ROUND(y2024.total_net_tax, 2) AS tax_2024,
    ROUND(y2025.total_net_tax, 2) AS tax_2025,
    ROUND(100.0 * (y2025.total_net_tax - y2024.total_net_tax) / y2024.total_net_tax, 2) AS pct_change
FROM tax_by_neighborhood_year y2024
JOIN tax_by_neighborhood_year y2025
  ON y2024.neighborhood = y2025.neighborhood
 AND y2024.tax_year = 2024
 AND y2025.tax_year = 2025
ORDER BY pct_change DESC;
```


Highlights:

- Biggest increases: Fischer Dev (+15.0%), St. Thomas Dev (+11.8%), Freret (+11.3%), Desire Area (+8.7%), Viavant-Venetian Isles (+7.0%)
- Biggest decreases: Central Business District (−8.9%), Iberville (−7.1%), Gert Town (−6.8%), West Lake Forest (−5.9%), Gentilly Woods (−4.2%)
- Most neighborhoods cluster within ±3%

## Using the data

The data is compressed and stored at `data/db/nola_tax.sqlite.xz` (xz gets it
under GitHub's 100MB file limit; gzip doesn't). Uncompress it with
`xz -d data/db/nola_tax.sqlite.xz` and use sqlite3 to query it. If you're
not super familiar with sql, or sqlite, i'd recommend just using a coding agent like claude or chatgpt and just ask it questions.
It will write good sql for you.


## Note on quality

I have not fully confirmed the quality here. I'm using this data for a separate project and I'll try to push updates here as I find problems.
There are likely a few problems because parsing PDFs isn't always perfect. I did ask the agent to iterate and spot check, and it seemed to find
and fix a lot of problems so I think it's usable at the moment. But not reliable to make conclusions yet.

