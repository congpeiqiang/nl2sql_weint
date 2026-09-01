SELECT 
    Album.Title AS AlbumTitle,
    Artist.Name AS ArtistName
FROM Album
INNER JOIN Artist ON Album.ArtistId = Artist.ArtistId
ORDER BY Artist.Name, Album.Title