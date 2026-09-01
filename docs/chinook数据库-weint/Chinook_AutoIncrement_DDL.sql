-- Database: Chinook_AutoIncrement
-- Export date: 2026-07-16 15:43:01
-- Total tables: 11

-- ============================================
-- Table: Album
-- ============================================
CREATE TABLE `Album` (
  `AlbumId` int NOT NULL AUTO_INCREMENT,
  `Title` varchar(160) COLLATE utf8mb4_unicode_ci NOT NULL,
  `ArtistId` int NOT NULL,
  PRIMARY KEY (`AlbumId`),
  KEY `IFK_AlbumArtistId` (`ArtistId`),
  CONSTRAINT `FK_AlbumArtistId` FOREIGN KEY (`ArtistId`) REFERENCES `Artist` (`ArtistId`)
) ENGINE=InnoDB AUTO_INCREMENT=348 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: Artist
-- ============================================
CREATE TABLE `Artist` (
  `ArtistId` int NOT NULL AUTO_INCREMENT,
  `Name` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`ArtistId`)
) ENGINE=InnoDB AUTO_INCREMENT=276 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: Customer
-- ============================================
CREATE TABLE `Customer` (
  `CustomerId` int NOT NULL AUTO_INCREMENT,
  `FirstName` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL,
  `LastName` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Company` varchar(80) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Address` varchar(70) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `City` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `State` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Country` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `PostalCode` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Phone` varchar(24) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fax` varchar(24) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Email` varchar(60) COLLATE utf8mb4_unicode_ci NOT NULL,
  `SupportRepId` int DEFAULT NULL,
  PRIMARY KEY (`CustomerId`),
  KEY `IFK_CustomerSupportRepId` (`SupportRepId`),
  CONSTRAINT `FK_CustomerSupportRepId` FOREIGN KEY (`SupportRepId`) REFERENCES `Employee` (`EmployeeId`)
) ENGINE=InnoDB AUTO_INCREMENT=60 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: Employee
-- ============================================
CREATE TABLE `Employee` (
  `EmployeeId` int NOT NULL AUTO_INCREMENT,
  `LastName` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `FirstName` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Title` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ReportsTo` int DEFAULT NULL,
  `BirthDate` datetime DEFAULT NULL,
  `HireDate` datetime DEFAULT NULL,
  `Address` varchar(70) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `City` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `State` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Country` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `PostalCode` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Phone` varchar(24) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fax` varchar(24) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Email` varchar(60) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`EmployeeId`),
  KEY `IFK_EmployeeReportsTo` (`ReportsTo`),
  CONSTRAINT `FK_EmployeeReportsTo` FOREIGN KEY (`ReportsTo`) REFERENCES `Employee` (`EmployeeId`)
) ENGINE=InnoDB AUTO_INCREMENT=9 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: Genre
-- ============================================
CREATE TABLE `Genre` (
  `GenreId` int NOT NULL AUTO_INCREMENT,
  `Name` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`GenreId`)
) ENGINE=InnoDB AUTO_INCREMENT=26 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: Invoice
-- ============================================
CREATE TABLE `Invoice` (
  `InvoiceId` int NOT NULL AUTO_INCREMENT,
  `CustomerId` int NOT NULL,
  `InvoiceDate` datetime NOT NULL,
  `BillingAddress` varchar(70) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `BillingCity` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `BillingState` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `BillingCountry` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `BillingPostalCode` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Total` decimal(10,2) NOT NULL,
  PRIMARY KEY (`InvoiceId`),
  KEY `IFK_InvoiceCustomerId` (`CustomerId`),
  CONSTRAINT `FK_InvoiceCustomerId` FOREIGN KEY (`CustomerId`) REFERENCES `Customer` (`CustomerId`)
) ENGINE=InnoDB AUTO_INCREMENT=413 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: InvoiceLine
-- ============================================
CREATE TABLE `InvoiceLine` (
  `InvoiceLineId` int NOT NULL AUTO_INCREMENT,
  `InvoiceId` int NOT NULL,
  `TrackId` int NOT NULL,
  `UnitPrice` decimal(10,2) NOT NULL,
  `Quantity` int NOT NULL,
  PRIMARY KEY (`InvoiceLineId`),
  KEY `IFK_InvoiceLineInvoiceId` (`InvoiceId`),
  KEY `IFK_InvoiceLineTrackId` (`TrackId`),
  CONSTRAINT `FK_InvoiceLineInvoiceId` FOREIGN KEY (`InvoiceId`) REFERENCES `Invoice` (`InvoiceId`),
  CONSTRAINT `FK_InvoiceLineTrackId` FOREIGN KEY (`TrackId`) REFERENCES `Track` (`TrackId`)
) ENGINE=InnoDB AUTO_INCREMENT=2241 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: MediaType
-- ============================================
CREATE TABLE `MediaType` (
  `MediaTypeId` int NOT NULL AUTO_INCREMENT,
  `Name` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`MediaTypeId`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: Playlist
-- ============================================
CREATE TABLE `Playlist` (
  `PlaylistId` int NOT NULL AUTO_INCREMENT,
  `Name` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`PlaylistId`)
) ENGINE=InnoDB AUTO_INCREMENT=19 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: PlaylistTrack
-- ============================================
CREATE TABLE `PlaylistTrack` (
  `PlaylistId` int NOT NULL,
  `TrackId` int NOT NULL,
  PRIMARY KEY (`PlaylistId`,`TrackId`),
  KEY `IFK_PlaylistTrackPlaylistId` (`PlaylistId`),
  KEY `IFK_PlaylistTrackTrackId` (`TrackId`),
  CONSTRAINT `FK_PlaylistTrackPlaylistId` FOREIGN KEY (`PlaylistId`) REFERENCES `Playlist` (`PlaylistId`),
  CONSTRAINT `FK_PlaylistTrackTrackId` FOREIGN KEY (`TrackId`) REFERENCES `Track` (`TrackId`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- Table: Track
-- ============================================
CREATE TABLE `Track` (
  `TrackId` int NOT NULL AUTO_INCREMENT,
  `Name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `AlbumId` int DEFAULT NULL,
  `MediaTypeId` int NOT NULL,
  `GenreId` int DEFAULT NULL,
  `Composer` varchar(220) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Milliseconds` int NOT NULL,
  `Bytes` int DEFAULT NULL,
  `UnitPrice` decimal(10,2) NOT NULL,
  PRIMARY KEY (`TrackId`),
  KEY `IFK_TrackAlbumId` (`AlbumId`),
  KEY `IFK_TrackGenreId` (`GenreId`),
  KEY `IFK_TrackMediaTypeId` (`MediaTypeId`),
  CONSTRAINT `FK_TrackAlbumId` FOREIGN KEY (`AlbumId`) REFERENCES `Album` (`AlbumId`),
  CONSTRAINT `FK_TrackGenreId` FOREIGN KEY (`GenreId`) REFERENCES `Genre` (`GenreId`),
  CONSTRAINT `FK_TrackMediaTypeId` FOREIGN KEY (`MediaTypeId`) REFERENCES `MediaType` (`MediaTypeId`)
) ENGINE=InnoDB AUTO_INCREMENT=3504 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
