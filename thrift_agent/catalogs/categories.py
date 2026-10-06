"""Poshmark's category → Depop's category path and Vinted's leaf (WO30).

1. A deterministic table first, written against the three catalogs: Poshmark department / category / subcategory →
   a Depop path ("Women > Bottoms > Skirts") and a Vinted leaf path ("Women > Clothing > Skirts", resolved to its id).
   A subcategory missing from the table takes its category's row ((dept, category, None)). REFINE rows split one
   Poshmark subcategory by the words of the item ("romper" vs "jumpsuit", "hoodie" vs "sweatshirt"). Kids rows name the
   Girls and the Boys leaf (Vinted's Kids tree is per gender; unisex goes on Girls, as Poshmark's size tab does).
2. No row (or a row that names nothing for this gender): Opus picks from the ENUMERATED leaves of that department — the
   tool's input is an enum of the catalog's ids/paths, so the answer can't be outside it.
3. Every model answer is cached (`learned_path()`: the private repo's category_map_learned.json) so the same input
   never asks twice.

A row whose target is missing from the current catalog (a refresh renamed it) is ignored: that item goes to the model."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, create_model

from thrift_agent import catalogs
from thrift_agent.catalogs import CatalogError

Kids = tuple[str | None, str | None]           # (girls sub-path, boys sub-path) under "Kids > Girls|Boys clothing > "
Spec = str | Kids | None


def _k(sub: str) -> Kids:
    return sub, sub


# (department, Poshmark category, subcategory or None) -> (Depop "Group > Category", Vinted path under the department)
TABLE: dict[tuple[str, str, str | None], tuple[str | None, Spec]] = {
    # ---------------- Women
    ("Women", "Tops", None): ("Tops > Other", "Clothing > Tops & T-shirts > Other tops & t-shirts"),
    ("Women", "Tops", "Blouses"): ("Tops > Blouses", "Clothing > Tops & T-shirts > Blouses"),
    ("Women", "Tops", "Bodysuits"): ("Tops > Bodysuits", "Clothing > Tops & T-shirts > Bodysuits"),
    ("Women", "Tops", "Button Down Shirts"): ("Tops > Shirts", "Clothing > Tops & T-shirts > Shirts"),
    ("Women", "Tops", "Camisoles"): ("Tops > Tank tops and camis", "Clothing > Tops & T-shirts > Camis"),
    ("Women", "Tops", "Crop Tops"): ("Tops > Crop tops", "Clothing > Tops & T-shirts > Crop tops"),
    ("Women", "Tops", "Jerseys"): ("Tops > Jerseys", "Clothing > Activewear > Jerseys"),
    ("Women", "Tops", "Muscle Tees"): ("Tops > Tank tops and camis", "Clothing > Tops & T-shirts > Tank tops"),
    ("Women", "Tops", "Sweatshirts & Hoodies"): ("Tops > Sweatshirts",
                                                 "Clothing > Jumpers & sweaters > Hoodies & sweatshirts"),
    ("Women", "Tops", "Tank Tops"): ("Tops > Tank tops and camis", "Clothing > Tops & T-shirts > Tank tops"),
    ("Women", "Tops", "Tees - Long Sleeve"): ("Tops > T-shirts", "Clothing > Tops & T-shirts > Long-sleeved tops"),
    ("Women", "Tops", "Tees - Short Sleeve"): ("Tops > T-shirts", "Clothing > Tops & T-shirts > T-shirts"),
    ("Women", "Tops", "Tunics"): ("Tops > Blouses", "Clothing > Tops & T-shirts > Tunics"),
    ("Women", "Sweaters", None): ("Tops > Sweaters", "Clothing > Jumpers & sweaters > Sweaters > Other sweaters"),
    ("Women", "Sweaters", "Cardigans"): ("Tops > Cardigans", "Clothing > Jumpers & sweaters > Cardigans"),
    ("Women", "Sweaters", "Cowl & Turtlenecks"): ("Tops > Sweaters",
                                                  "Clothing > Jumpers & sweaters > Sweaters > Turtleneck sweaters"),
    ("Women", "Sweaters", "Crew & Scoop Necks"): ("Tops > Sweaters",
                                                  "Clothing > Jumpers & sweaters > Sweaters > Knitted sweaters"),
    ("Women", "Sweaters", "Shrugs & Ponchos"): ("Tops > Cardigans", "Clothing > Jumpers & sweaters > Boleros"),
    ("Women", "Sweaters", "V-Necks"): ("Tops > Sweaters", "Clothing > Jumpers & sweaters > Sweaters > V-neck sweaters"),
    ("Women", "Dresses", None): ("Dresses > Dresses", "Clothing > Dresses > Other dresses"),
    ("Women", "Dresses", "Backless"): ("Dresses > Dresses", "Clothing > Dresses > Special-occasion dresses > Backless dresses"),
    ("Women", "Dresses", "Maxi"): ("Dresses > Dresses", "Clothing > Dresses > Long dresses"),
    ("Women", "Dresses", "Midi"): ("Dresses > Dresses", "Clothing > Dresses > Midi-dresses"),
    ("Women", "Dresses", "Mini"): ("Dresses > Dresses", "Clothing > Dresses > Mini-dresses"),
    ("Women", "Dresses", "Prom"): ("Dresses > Prom dresses", "Clothing > Dresses > Special-occasion dresses > Prom dresses"),
    ("Women", "Dresses", "Strapless"): ("Dresses > Dresses", "Clothing > Dresses > Strapless dresses"),
    ("Women", "Dresses", "Wedding"): ("Dresses > Wedding dresses",
                                      "Clothing > Dresses > Special-occasion dresses > Wedding dresses"),
    ("Women", "Skirts", None): ("Bottoms > Skirts", "Clothing > Skirts"),
    ("Women", "Shorts", None): ("Bottoms > Shorts", "Clothing > Shorts & cropped pants > Other shorts & cropped pants"),
    ("Women", "Shorts", "Athletic Shorts"): ("Bottoms > Shorts", "Clothing > Activewear > Shorts"),
    ("Women", "Shorts", "Bermudas"): ("Bottoms > Shorts", "Clothing > Shorts & cropped pants > Knee-length shorts"),
    ("Women", "Shorts", "Bike Shorts"): ("Bottoms > Shorts", "Clothing > Activewear > Shorts"),
    ("Women", "Shorts", "Cargos"): ("Bottoms > Shorts", "Clothing > Shorts & cropped pants > Cargo shorts"),
    ("Women", "Shorts", "High Waist"): ("Bottoms > Shorts", "Clothing > Shorts & cropped pants > High-waist shorts"),
    ("Women", "Shorts", "Jean Shorts"): ("Bottoms > Shorts", "Clothing > Shorts & cropped pants > Jean shorts"),
    ("Women", "Shorts", "Skorts"): ("Bottoms > Skirts", "Clothing > Skorts"),
    ("Women", "Jeans", None): ("Bottoms > Jeans", "Clothing > Jeans > Other"),
    ("Women", "Jeans", "Ankle & Cropped"): ("Bottoms > Jeans", "Clothing > Jeans > Cropped jeans"),
    ("Women", "Jeans", "Boot Cut"): ("Bottoms > Jeans", "Clothing > Jeans > Flared jeans"),
    ("Women", "Jeans", "Boyfriend"): ("Bottoms > Jeans", "Clothing > Jeans > Boyfriend jeans"),
    ("Women", "Jeans", "Flare & Wide Leg"): ("Bottoms > Jeans", "Clothing > Jeans > Flared jeans"),
    ("Women", "Jeans", "High Rise"): ("Bottoms > Jeans", "Clothing > Jeans > High-waisted jeans"),
    ("Women", "Jeans", "Jeggings"): ("Bottoms > Jeans", "Clothing > Jeans > Skinny jeans"),
    ("Women", "Jeans", "Overalls"): ("Jumpsuits and rompers > Overalls",
                                     "Clothing > Jumpsuits & rompers > Other jumpsuits & rompers"),
    ("Women", "Jeans", "Skinny"): ("Bottoms > Jeans", "Clothing > Jeans > Skinny jeans"),
    ("Women", "Jeans", "Straight Leg"): ("Bottoms > Jeans", "Clothing > Jeans > Straight jeans"),
    ("Women", "Pants & Jumpsuits", None): ("Bottoms > Pants", "Clothing > Pants & leggings > Other trousers"),
    ("Women", "Pants & Jumpsuits", "Ankle & Cropped"): ("Bottoms > Pants",
                                                        "Clothing > Pants & leggings > Cropped pants & chinos"),
    ("Women", "Pants & Jumpsuits", "Capris"): ("Bottoms > Pants", "Clothing > Shorts & cropped pants > Cropped pants"),
    ("Women", "Pants & Jumpsuits", "Jumpsuits & Rompers"): ("Jumpsuits and rompers > Jumpsuits",
                                                            "Clothing > Jumpsuits & rompers > Jumpsuits"),
    ("Women", "Pants & Jumpsuits", "Leggings"): ("Bottoms > Leggings", "Clothing > Pants & leggings > Leggings"),
    ("Women", "Pants & Jumpsuits", "Pantsuits"): ("Suits > Suits", "Clothing > Suits & blazers > Pantsuits"),
    ("Women", "Pants & Jumpsuits", "Skinny"): ("Bottoms > Pants", "Clothing > Pants & leggings > Skinny pants"),
    ("Women", "Pants & Jumpsuits", "Straight Leg"): ("Bottoms > Pants",
                                                     "Clothing > Pants & leggings > Straight-leg pants"),
    ("Women", "Pants & Jumpsuits", "Track Pants & Joggers"): ("Bottoms > Sweatpants", "Clothing > Activewear > Pants"),
    ("Women", "Pants & Jumpsuits", "Trousers"): ("Bottoms > Pants", "Clothing > Pants & leggings > Tailored pants"),
    ("Women", "Pants & Jumpsuits", "Wide Leg"): ("Bottoms > Pants", "Clothing > Pants & leggings > Wide-leg pants"),
    ("Women", "Jackets & Coats", None): ("Coats and jackets > Jackets", None),
    ("Women", "Jackets & Coats", "Blazers & Suit Jackets"): ("Suits > Tailored jackets",
                                                              "Clothing > Suits & blazers > Blazers"),
    ("Women", "Jackets & Coats", "Bomber Jackets"): ("Coats and jackets > Jackets",
                                                     "Clothing > Outerwear > Jackets > Bomber jackets"),
    ("Women", "Jackets & Coats", "Capes"): ("Coats and jackets > Jackets", "Clothing > Outerwear > Capes & ponchos"),
    ("Women", "Jackets & Coats", "Jean Jackets"): ("Coats and jackets > Jackets",
                                                   "Clothing > Outerwear > Jackets > Denim jackets"),
    ("Women", "Jackets & Coats", "Leather Jackets"): ("Coats and jackets > Jackets",
                                                      "Clothing > Outerwear > Jackets > Biker & racer jackets"),
    ("Women", "Jackets & Coats", "Pea Coats"): ("Coats and jackets > Coats", "Clothing > Outerwear > Coats > Peacoats"),
    ("Women", "Jackets & Coats", "Puffers"): ("Coats and jackets > Coats", "Clothing > Outerwear > Jackets > Puffer jackets"),
    ("Women", "Jackets & Coats", "Ski & Snow Jackets"): ("Coats and jackets > Jackets",
                                                         "Clothing > Outerwear > Jackets > Ski & snowboard jackets"),
    ("Women", "Jackets & Coats", "Teddy Jackets"): ("Coats and jackets > Coats",
                                                    "Clothing > Outerwear > Jackets > Fleece jackets"),
    ("Women", "Jackets & Coats", "Trench Coats"): ("Coats and jackets > Coats", "Clothing > Outerwear > Coats > Trench coats"),
    ("Women", "Jackets & Coats", "Utility Jackets"): ("Coats and jackets > Jackets",
                                                      "Clothing > Outerwear > Jackets > Field & utility jackets"),
    ("Women", "Jackets & Coats", "Varsity Jackets"): ("Coats and jackets > Jackets",
                                                      "Clothing > Outerwear > Jackets > Varsity jackets"),
    ("Women", "Jackets & Coats", "Vests"): ("Coats and jackets > Vests", "Clothing > Outerwear > Vests"),
    ("Women", "Intimates & Sleepwear", None): ("Underwear > Other", "Clothing > Lingerie & nightwear > Other"),
    ("Women", "Intimates & Sleepwear", "Bandeaus"): ("Underwear > Bandeaus", "Clothing > Lingerie & nightwear > Bras"),
    ("Women", "Intimates & Sleepwear", "Bras"): ("Underwear > Bras", "Clothing > Lingerie & nightwear > Bras"),
    ("Women", "Intimates & Sleepwear", "Chemises & Slips"): ("Sleepwear > Other",
                                                             "Clothing > Lingerie & nightwear > Sleepwear"),
    ("Women", "Intimates & Sleepwear", "Pajamas"): ("Sleepwear > Pajamas", "Clothing > Lingerie & nightwear > Sleepwear"),
    ("Women", "Intimates & Sleepwear", "Panties"): ("Underwear > Panties", "Clothing > Lingerie & nightwear > Panties"),
    ("Women", "Intimates & Sleepwear", "Robes"): ("Sleepwear > Robes", "Clothing > Lingerie & nightwear > Dressing gowns"),
    ("Women", "Intimates & Sleepwear", "Shapewear"): ("Underwear > Shapewear",
                                                      "Clothing > Lingerie & nightwear > Shapewear"),
    ("Women", "Intimates & Sleepwear", "Sports Bras"): ("Underwear > Bras", "Clothing > Activewear > Sports bras"),
    ("Women", "Swim", None): ("Swimwear > Other", "Clothing > Swimwear > Other swimwear & beachwear"),
    ("Women", "Swim", "Bikinis"): ("Swimwear > Bikini and tankini sets", "Clothing > Swimwear > Bikinis & tankinis"),
    ("Women", "Swim", "Coverups"): ("Swimwear > Cover ups", "Clothing > Swimwear > Cover-ups & sarongs"),
    ("Women", "Swim", "One Pieces"): ("Swimwear > Swimsuits", "Clothing > Swimwear > One-pieces"),
    ("Women", "Swim", "Sarongs"): ("Swimwear > Cover ups", "Clothing > Swimwear > Cover-ups & sarongs"),
    ("Women", "Shoes", None): ("Footwear > Other", None),
    ("Women", "Shoes", "Ankle Boots & Booties"): ("Footwear > Boots", "Shoes > Boots > Ankle boots"),
    ("Women", "Shoes", "Athletic Shoes"): ("Footwear > Sneakers", "Shoes > Sneakers"),
    ("Women", "Shoes", "Combat & Moto Boots"): ("Footwear > Boots", "Shoes > Boots > Ankle boots"),
    ("Women", "Shoes", "Espadrilles"): ("Footwear > Espadrilles", "Shoes > Espadrilles"),
    ("Women", "Shoes", "Flats & Loafers"): ("Footwear > Ballet shoes", "Shoes > Ballerinas"),
    ("Women", "Shoes", "Heeled Boots"): ("Footwear > Boots", "Shoes > Boots > Ankle boots"),
    ("Women", "Shoes", "Heels"): ("Footwear > Pumps", "Shoes > Heels"),
    ("Women", "Shoes", "Lace Up Boots"): ("Footwear > Boots", "Shoes > Boots > Ankle boots"),
    ("Women", "Shoes", "Moccasins"): ("Footwear > Loafers", "Shoes > Boat shoes, loafers & moccasins"),
    ("Women", "Shoes", "Mules & Clogs"): ("Footwear > Mules", "Shoes > Clogs & mules"),
    ("Women", "Shoes", "Over the Knee Boots"): ("Footwear > Boots", "Shoes > Boots > Over-the-knee boots"),
    ("Women", "Shoes", "Platforms"): ("Footwear > Other", "Shoes > Heels"),
    ("Women", "Shoes", "Sandals"): ("Footwear > Sandals", "Shoes > Sandals"),
    ("Women", "Shoes", "Slippers"): ("Footwear > Slippers", "Shoes > Slippers"),
    ("Women", "Shoes", "Sneakers"): ("Footwear > Sneakers", "Shoes > Sneakers"),
    ("Women", "Shoes", "Wedges"): ("Footwear > Other", "Shoes > Heels"),
    ("Women", "Shoes", "Winter & Rain Boots"): ("Footwear > Boots", "Shoes > Boots > Snow boots"),
    ("Women", "Bags", None): ("Accessories > Bags", "Bags > Handbags"),
    ("Women", "Bags", "Baby Bags"): ("Accessories > Bags", "Bags > Tote bags"),
    ("Women", "Bags", "Backpacks"): ("Accessories > Bags", "Bags > Backpacks"),
    ("Women", "Bags", "Clutches & Wristlets"): ("Accessories > Bags", "Bags > Clutches"),
    ("Women", "Bags", "Cosmetic Bags & Cases"): ("Accessories > Bags", "Bags > Makeup bags"),
    ("Women", "Bags", "Crossbody Bags"): ("Accessories > Bags", "Bags > Shoulder bags"),
    ("Women", "Bags", "Hobos"): ("Accessories > Bags", "Bags > Hobo bags"),
    ("Women", "Bags", "Laptop Bags"): ("Accessories > Bags", "Bags > Briefcases"),
    ("Women", "Bags", "Mini Bags"): ("Accessories > Bags", "Bags > Handbags"),
    ("Women", "Bags", "Satchels"): ("Accessories > Bags", "Bags > Satchels & messenger bags"),
    ("Women", "Bags", "Shoulder Bags"): ("Accessories > Bags", "Bags > Shoulder bags"),
    ("Women", "Bags", "Totes"): ("Accessories > Bags", "Bags > Tote bags"),
    ("Women", "Bags", "Travel Bags"): ("Accessories > Bags", "Bags > Holdalls & duffel bags"),
    ("Women", "Bags", "Wallets"): ("Accessories > Wallets and cardholders", "Bags > Wallets"),
    ("Women", "Accessories", None): ("Accessories > Other", "Accessories > Other accessories"),
    ("Women", "Accessories", "Belts"): ("Accessories > Belts", "Accessories > Belts"),
    ("Women", "Accessories", "Gloves & Mittens"): ("Accessories > Gloves", "Accessories > Gloves"),
    ("Women", "Accessories", "Hair Accessories"): ("Accessories > Hair accessories", "Accessories > Hair accessories"),
    ("Women", "Accessories", "Hats"): ("Accessories > Hats and caps", "Accessories > Hats & caps > Hats"),
    ("Women", "Accessories", "Hosiery & Socks"): ("Underwear > Socks", "Clothing > Lingerie & nightwear > Socks"),
    ("Women", "Accessories", "Key & Card Holders"): ("Accessories > Wallets and cardholders", "Bags > Wallets"),
    ("Women", "Accessories", "Scarves & Wraps"): ("Accessories > Scarves and wraps", "Accessories > Scarves & shawls"),
    ("Women", "Accessories", "Sunglasses"): ("Accessories > Sunglasses", "Accessories > Sunglasses"),
    ("Women", "Accessories", "Umbrellas"): ("Accessories > Other", "Accessories > Umbrellas"),
    ("Women", "Accessories", "Watches"): ("Accessories > Watches", "Accessories > Watches"),
    ("Women", "Jewelry", None): ("Accessories > Jewelry", "Accessories > Jewelry > Other jewelry"),
    ("Women", "Jewelry", "Bracelets"): ("Accessories > Jewelry", "Accessories > Jewelry > Bracelets"),
    ("Women", "Jewelry", "Brooches"): ("Accessories > Jewelry", "Accessories > Jewelry > Brooches"),
    ("Women", "Jewelry", "Earrings"): ("Accessories > Jewelry", "Accessories > Jewelry > Earrings"),
    ("Women", "Jewelry", "Necklaces"): ("Accessories > Jewelry", "Accessories > Jewelry > Necklaces"),
    ("Women", "Jewelry", "Rings"): ("Accessories > Jewelry", "Accessories > Jewelry > Rings"),
    # ---------------- Men
    ("Men", "Shirts", None): ("Tops > Other", "Clothing > Tops & T-shirts > T-shirts > Other T-shirts"),
    ("Men", "Shirts", "Casual Button Down Shirts"): ("Tops > Shirts", "Clothing > Tops & T-shirts > Shirts > Other shirts"),
    ("Men", "Shirts", "Dress Shirts"): ("Tops > Shirts", "Clothing > Tops & T-shirts > Shirts > Other shirts"),
    ("Men", "Shirts", "Jerseys"): ("Tops > Jerseys", "Clothing > Activewear > Jerseys"),
    ("Men", "Shirts", "Polos"): ("Tops > Polo shirts", "Clothing > Tops & T-shirts > Polo shirts"),
    ("Men", "Shirts", "Sweatshirts & Hoodies"): ("Tops > Sweatshirts",
                                                 "Clothing > Sweaters & sweatshirts > Hoodies & sweatshirts"),
    ("Men", "Shirts", "Tank Tops"): ("Tops > Tank tops and camis", "Clothing > Tops & T-shirts > Tank tops"),
    ("Men", "Shirts", "Tees - Long Sleeve"): ("Tops > T-shirts", "Clothing > Tops & T-shirts > T-shirts > Long-sleeved T-shirts"),
    ("Men", "Shirts", "Tees - Short Sleeve"): ("Tops > T-shirts", "Clothing > Tops & T-shirts > T-shirts > Other T-shirts"),
    ("Men", "Sweaters", None): ("Tops > Sweaters", "Clothing > Sweaters & sweatshirts > Sweaters"),
    ("Men", "Sweaters", "Cardigan"): ("Tops > Cardigans", "Clothing > Sweaters & sweatshirts > Cardigans"),
    ("Men", "Sweaters", "Crewneck"): ("Tops > Sweaters", "Clothing > Sweaters & sweatshirts > Crew neck sweaters"),
    ("Men", "Sweaters", "Turtleneck"): ("Tops > Sweaters", "Clothing > Sweaters & sweatshirts > Turtleneck sweaters"),
    ("Men", "Sweaters", "V-Neck"): ("Tops > Sweaters", "Clothing > Sweaters & sweatshirts > V-neck sweaters"),
    ("Men", "Sweaters", "Zip Up"): ("Tops > Sweaters",
                                    "Clothing > Sweaters & sweatshirts > Zip-up hoodies & sweatshirts"),
    ("Men", "Jeans", None): ("Bottoms > Jeans", None),
    ("Men", "Jeans", "Bootcut"): ("Bottoms > Jeans", "Clothing > Jeans > Straight fit jeans"),
    ("Men", "Jeans", "Relaxed"): ("Bottoms > Jeans", "Clothing > Jeans > Straight fit jeans"),
    ("Men", "Jeans", "Skinny"): ("Bottoms > Jeans", "Clothing > Jeans > Skinny jeans"),
    ("Men", "Jeans", "Slim"): ("Bottoms > Jeans", "Clothing > Jeans > Slim fit jeans"),
    ("Men", "Jeans", "Slim Straight"): ("Bottoms > Jeans", "Clothing > Jeans > Slim fit jeans"),
    ("Men", "Jeans", "Straight"): ("Bottoms > Jeans", "Clothing > Jeans > Straight fit jeans"),
    ("Men", "Pants", None): ("Bottoms > Pants", "Clothing > Pants > Other pants"),
    ("Men", "Pants", "Chinos & Khakis"): ("Bottoms > Pants", "Clothing > Pants > Chinos"),
    ("Men", "Pants", "Dress"): ("Bottoms > Pants", "Clothing > Pants > Tailored pants"),
    ("Men", "Pants", "Sweatpants & Joggers"): ("Bottoms > Sweatpants", "Clothing > Pants > Sweatpants"),
    ("Men", "Shorts", None): ("Bottoms > Shorts", "Clothing > Shorts > Other shorts"),
    ("Men", "Shorts", "Athletic"): ("Bottoms > Shorts", "Clothing > Activewear > Shorts"),
    ("Men", "Shorts", "Cargo"): ("Bottoms > Shorts", "Clothing > Shorts > Cargo shorts"),
    ("Men", "Shorts", "Flat Front"): ("Bottoms > Shorts", "Clothing > Shorts > Chino shorts"),
    ("Men", "Shorts", "Jean Shorts"): ("Bottoms > Shorts", "Clothing > Shorts > Denim shorts"),
    ("Men", "Jackets & Coats", None): ("Coats and jackets > Jackets", None),
    ("Men", "Jackets & Coats", "Bomber & Varsity"): ("Coats and jackets > Jackets",
                                                     "Clothing > Outerwear > Jackets > Bomber jackets"),
    ("Men", "Jackets & Coats", "Lightweight & Shirt Jackets"): ("Coats and jackets > Jackets",
                                                                "Clothing > Outerwear > Jackets > Shackets"),
    ("Men", "Jackets & Coats", "Military & Field"): ("Coats and jackets > Jackets",
                                                     "Clothing > Outerwear > Jackets > Field & utility jackets"),
    ("Men", "Jackets & Coats", "Pea Coats"): ("Coats and jackets > Coats", "Clothing > Outerwear > Coats > Peacoats"),
    ("Men", "Jackets & Coats", "Performance Jackets"): ("Coats and jackets > Jackets", "Clothing > Activewear > Outerwear"),
    ("Men", "Jackets & Coats", "Puffers"): ("Coats and jackets > Coats", "Clothing > Outerwear > Jackets > Puffer jackets"),
    ("Men", "Jackets & Coats", "Raincoats"): ("Coats and jackets > Coats", "Clothing > Outerwear > Coats > Raincoats"),
    ("Men", "Jackets & Coats", "Ski & Snowboard"): ("Coats and jackets > Jackets",
                                                    "Clothing > Outerwear > Jackets > Ski & snowboard jackets"),
    ("Men", "Jackets & Coats", "Trench Coats"): ("Coats and jackets > Coats", "Clothing > Outerwear > Coats > Trench coats"),
    ("Men", "Jackets & Coats", "Vests"): ("Coats and jackets > Vests", "Clothing > Outerwear > Vests"),
    ("Men", "Jackets & Coats", "Windbreakers"): ("Coats and jackets > Jackets", "Clothing > Outerwear > Jackets > Windbreakers"),
    ("Men", "Suits & Blazers", None): ("Suits > Other", "Clothing > Suits & blazers > Other suits & blazers"),
    ("Men", "Suits & Blazers", "Sport Coats & Blazers"): ("Suits > Tailored jackets",
                                                          "Clothing > Suits & blazers > Suit jackets & blazers"),
    ("Men", "Suits & Blazers", "Suits"): ("Suits > Suits", "Clothing > Suits & blazers > Suit sets"),
    ("Men", "Suits & Blazers", "Tuxedos"): ("Suits > Tuxedos", "Clothing > Suits & blazers > Suit sets"),
    ("Men", "Suits & Blazers", "Vests"): ("Suits > Vests", "Clothing > Suits & blazers > Vests"),
    ("Men", "Shoes", None): ("Footwear > Other", None),
    ("Men", "Shoes", "Athletic Shoes"): ("Footwear > Sneakers", "Shoes > Sneakers"),
    ("Men", "Shoes", "Boat Shoes"): ("Footwear > Boat shoes", "Shoes > Boat shoes, loafers & moccasins"),
    ("Men", "Shoes", "Boots"): ("Footwear > Boots", "Shoes > Boots > Desert & lace-up boots"),
    ("Men", "Shoes", "Chukka Boots"): ("Footwear > Boots", "Shoes > Boots > Desert & lace-up boots"),
    ("Men", "Shoes", "Cowboy & Western Boots"): ("Footwear > Boots", "Shoes > Boots > Chelsea & slip-on boots"),
    ("Men", "Shoes", "Loafers & Slip-Ons"): ("Footwear > Loafers", "Shoes > Boat shoes, loafers & moccasins"),
    ("Men", "Shoes", "Oxfords & Derbys"): ("Footwear > Oxfords", "Shoes > Formal shoes"),
    ("Men", "Shoes", "Rain & Snow Boots"): ("Footwear > Boots", "Shoes > Boots > Snow boots"),
    ("Men", "Shoes", "Sandals & Flip-Flops"): ("Footwear > Sandals", "Shoes > Sandals"),
    ("Men", "Shoes", "Sneakers"): ("Footwear > Sneakers", "Shoes > Sneakers"),
    ("Men", "Bags", None): ("Accessories > Bags", "Accessories > Bags & backpacks > Shoulder bags"),
    ("Men", "Bags", "Backpacks"): ("Accessories > Bags", "Accessories > Bags & backpacks > Backpacks"),
    ("Men", "Bags", "Belt Bags"): ("Accessories > Bags", "Accessories > Bags & backpacks > Fanny packs"),
    ("Men", "Bags", "Briefcases"): ("Accessories > Bags", "Accessories > Bags & backpacks > Briefcases"),
    ("Men", "Bags", "Duffel Bags"): ("Accessories > Bags", "Accessories > Bags & backpacks > Holdalls & duffel bags"),
    ("Men", "Bags", "Laptop Bags"): ("Accessories > Bags", "Accessories > Bags & backpacks > Briefcases"),
    ("Men", "Bags", "Luggage & Travel Bags"): ("Accessories > Bags",
                                               "Accessories > Bags & backpacks > Luggage & suitcases"),
    ("Men", "Bags", "Messenger Bags"): ("Accessories > Bags",
                                        "Accessories > Bags & backpacks > Satchels & messenger bags"),
    ("Men", "Bags", "Wallets"): ("Accessories > Wallets and cardholders", "Accessories > Bags & backpacks > Wallets"),
    ("Men", "Accessories", None): ("Accessories > Other", "Accessories > Other accessories"),
    ("Men", "Accessories", "Belts"): ("Accessories > Belts", "Accessories > Belts"),
    ("Men", "Accessories", "Cuff Links"): ("Accessories > Jewelry", "Accessories > Jewelry > Cufflinks"),
    ("Men", "Accessories", "Gloves"): ("Accessories > Gloves", "Accessories > Gloves"),
    ("Men", "Accessories", "Hats"): ("Accessories > Hats and caps", "Accessories > Hats & caps > Hats"),
    ("Men", "Accessories", "Jewelry"): ("Accessories > Jewelry", "Accessories > Jewelry > Other jewelry"),
    ("Men", "Accessories", "Key & Card Holders"): ("Accessories > Wallets and cardholders",
                                                   "Accessories > Bags & backpacks > Wallets"),
    ("Men", "Accessories", "Pocket Squares"): ("Accessories > Other", "Accessories > Pocket squares"),
    ("Men", "Accessories", "Scarves"): ("Accessories > Scarves and wraps", "Accessories > Scarves & shawls"),
    ("Men", "Accessories", "Sunglasses"): ("Accessories > Sunglasses", "Accessories > Sunglasses"),
    ("Men", "Accessories", "Suspenders"): ("Accessories > Other", "Accessories > Suspenders"),
    ("Men", "Accessories", "Ties"): ("Accessories > Other", "Accessories > Ties & bow ties"),
    ("Men", "Accessories", "Watches"): ("Accessories > Watches", "Accessories > Watches"),
    ("Men", "Swim", None): ("Swimwear > Swim briefs and shorts", "Clothing > Swimwear"),
    ("Men", "Swim", "Rash Guards"): ("Swimwear > Other", "Clothing > Swimwear"),
    ("Men", "Underwear & Socks", None): ("Underwear > Other", "Clothing > Socks & underwear > Other socks & underwear"),
    ("Men", "Underwear & Socks", "Athletic Socks"): ("Underwear > Socks", "Clothing > Socks & underwear > Socks"),
    ("Men", "Underwear & Socks", "Casual Socks"): ("Underwear > Socks", "Clothing > Socks & underwear > Socks"),
    ("Men", "Underwear & Socks", "Dress Socks"): ("Underwear > Socks", "Clothing > Socks & underwear > Socks"),
    ("Men", "Underwear & Socks", "Boxer Briefs"): ("Underwear > Boxers and briefs",
                                                   "Clothing > Socks & underwear > Underwear"),
    ("Men", "Underwear & Socks", "Boxers"): ("Underwear > Boxers and briefs", "Clothing > Socks & underwear > Underwear"),
    ("Men", "Underwear & Socks", "Briefs"): ("Underwear > Boxers and briefs", "Clothing > Socks & underwear > Underwear"),
    ("Men", "Underwear & Socks", "Undershirts"): ("Underwear > Undershirts", "Clothing > Socks & underwear > Underwear"),
    # ---------------- Kids (Vinted: (girls, boys) sub-paths)
    ("Kids", "Shirts & Tops", None): ("Tops > Other", _k("Tops & T-shirts > Other tops & T-shirts")),
    ("Kids", "Shirts & Tops", "Blouses"): ("Tops > Blouses", _k("Tops & T-shirts > Shirts")),
    ("Kids", "Shirts & Tops", "Button Down Shirts"): ("Tops > Shirts", _k("Tops & T-shirts > Shirts")),
    ("Kids", "Shirts & Tops", "Camisoles"): ("Tops > Tank tops and camis", _k("Tops & T-shirts > Sleeveless tops")),
    ("Kids", "Shirts & Tops", "Jerseys"): ("Tops > Jerseys", _k("Activewear")),
    ("Kids", "Shirts & Tops", "Polos"): ("Tops > Polo shirts", _k("Tops & T-shirts > Polo shirts")),
    ("Kids", "Shirts & Tops", "Sweaters"): ("Tops > Sweaters", _k("Sweaters & hoodies > Sweaters")),
    ("Kids", "Shirts & Tops", "Sweatshirts & Hoodies"): ("Tops > Sweatshirts",
                                                         _k("Sweaters & hoodies > Hoodies & sweatshirts")),
    ("Kids", "Shirts & Tops", "Tank Tops"): ("Tops > Tank tops and camis", _k("Tops & T-shirts > Sleeveless tops")),
    ("Kids", "Shirts & Tops", "Tees - Long Sleeve"): ("Tops > T-shirts", _k("Tops & T-shirts > Long-sleeved tops")),
    ("Kids", "Shirts & Tops", "Tees - Short Sleeve"): ("Tops > T-shirts", _k("Tops & T-shirts > T-shirts")),
    ("Kids", "Bottoms", None): ("Bottoms > Pants", _k("Pants, shorts & overalls > Other pants, shorts & overalls")),
    ("Kids", "Bottoms", "Jeans"): ("Bottoms > Jeans", _k("Pants, shorts & overalls > Jeans")),
    ("Kids", "Bottoms", "Jumpsuits & Rompers"): ("Jumpsuits and rompers > Jumpsuits",
                                                 _k("Pants, shorts & overalls > Jumpsuits & overalls")),
    ("Kids", "Bottoms", "Leggings"): ("Bottoms > Leggings", _k("Pants, shorts & overalls > Leggings")),
    ("Kids", "Bottoms", "Overalls"): ("Jumpsuits and rompers > Overalls",
                                      _k("Pants, shorts & overalls > Jumpsuits & overalls")),
    ("Kids", "Bottoms", "Shorts"): ("Bottoms > Shorts", _k("Pants, shorts & overalls > Shorts & cropped pants")),
    ("Kids", "Bottoms", "Skirts"): ("Bottoms > Skirts", ("Skirts", "Other boys' clothing")),
    ("Kids", "Bottoms", "Skorts"): ("Bottoms > Skirts", ("Skirts", "Other boys' clothing")),
    ("Kids", "Bottoms", "Sweatpants & Joggers"): ("Bottoms > Sweatpants",
                                                  _k("Pants, shorts & overalls > Other pants, shorts & overalls")),
    ("Kids", "Dresses", None): ("Dresses > Casual dresses", ("Dresses > Short dresses", "Other boys' clothing")),
    ("Kids", "Dresses", "Formal"): ("Dresses > Formal dresses", ("Dresses > Short dresses", "Other boys' clothing")),
    ("Kids", "Jackets & Coats", None): ("Coats and jackets > Jackets", None),
    ("Kids", "Jackets & Coats", "Blazers"): ("Suits > Tailored jackets", _k("Outerwear > Jackets > Blazers")),
    ("Kids", "Jackets & Coats", "Capes"): ("Coats and jackets > Coats", _k("Outerwear > Rain gear > Ponchos")),
    ("Kids", "Jackets & Coats", "Jean Jackets"): ("Coats and jackets > Jackets", _k("Outerwear > Jackets > Denim jackets")),
    ("Kids", "Jackets & Coats", "Pea Coats"): ("Coats and jackets > Coats", _k("Outerwear > Coats > Peacoats")),
    ("Kids", "Jackets & Coats", "Puffers"): ("Coats and jackets > Coats", _k("Outerwear > Jackets > Puffer jackets")),
    ("Kids", "Jackets & Coats", "Raincoats"): ("Coats and jackets > Coats", _k("Outerwear > Rain gear > Raincoats")),
    ("Kids", "Jackets & Coats", "Vests"): ("Coats and jackets > Vests", _k("Outerwear > Vests")),
    ("Kids", "Matching Sets", None): ("Tops > Other", ("Other girls' clothing", "Other boys' clothing")),
    ("Kids", "One Pieces", None): ("Tops > Bodysuits", ("Baby clothing > Bodysuits", "Baby boys' clothing > Bodysuits")),
    ("Kids", "One Pieces", "Footies"): ("Onesies and sleepers > Onesies and sleepers",
                                        ("Sleepwear & nightwear > One-piece pajamas", "Sleepwear > One-piece pajamas")),
    ("Kids", "Pajamas", None): ("Sleepwear > Pajamas",
                                ("Sleepwear & nightwear > Two-piece pajamas", "Sleepwear > Two-piece pajamas")),
    ("Kids", "Pajamas", "Nightgowns"): ("Sleepwear > Other",
                                        ("Sleepwear & nightwear > Nightgowns", "Sleepwear > One-piece pajamas")),
    ("Kids", "Pajamas", "Robes"): ("Sleepwear > Robes", _k("Swimwear > Bathrobes")),
    ("Kids", "Pajamas", "Sleep Sacks"): ("Onesies and sleepers > Onesies and sleepers",
                                         ("Sleepwear & nightwear > One-piece pajamas", "Sleepwear > One-piece pajamas")),
    ("Kids", "Shoes", None): ("Footwear > Other", None),
    ("Kids", "Shoes", "Baby & Walker"): ("Footwear > Baby shoes", _k("Shoes > Baby shoes")),
    ("Kids", "Shoes", "Boots"): ("Footwear > Boots", _k("Shoes > Boots > Ankle boots")),
    ("Kids", "Shoes", "Dress Shoes"): ("Footwear > Other", _k("Shoes > Formal & special occasion shoes")),
    ("Kids", "Shoes", "Moccasins"): ("Footwear > Loafers", (None, "Shoes > Boat shoes, loafers & moccasins")),
    ("Kids", "Shoes", "Rain & Snow Boots"): ("Footwear > Boots", _k("Shoes > Boots > Snow boots")),
    ("Kids", "Shoes", "Sandals & Flip Flops"): ("Footwear > Sandals", _k("Shoes > Flip-flops, sandals & slides > Sandals")),
    ("Kids", "Shoes", "Slippers"): ("Footwear > Slippers", _k("Shoes > Slippers")),
    ("Kids", "Shoes", "Sneakers"): ("Footwear > Sneakers", _k("Shoes > Sneakers > Lace-up sneakers")),
    ("Kids", "Shoes", "Water Shoes"): ("Footwear > Other", ("Shoes > Sports shoes > Swimming & water shoes",
                                                            "Shoes > Sport shoes > Swimming & water shoes")),
    ("Kids", "Swim", None): ("Swimwear > Other", None),
    ("Kids", "Swim", "Bikinis"): ("Swimwear > Bikini and tankini sets", ("Swimwear > Bikinis & tankinis", None)),
    ("Kids", "Swim", "One Piece"): ("Swimwear > Swimsuits", ("Swimwear > One-piece swimsuits", None)),
    ("Kids", "Swim", "Swim Trunks"): ("Swimwear > Swim briefs and shorts", (None, "Swimwear > Swimming trunks")),
    ("Kids", "Swim", "Rashguards"): ("Swimwear > Other", _k("Activewear")),
    ("Kids", "Swim", "Coverups"): ("Swimwear > Cover ups", ("Other girls' clothing", "Other boys' clothing")),
    ("Kids", "Accessories", None): ("Accessories > Other", _k("Accessories > Other accessories")),
    ("Kids", "Accessories", "Bags"): ("Accessories > Bags", _k("Bags & backpacks")),
    ("Kids", "Accessories", "Belts"): ("Accessories > Belts", _k("Accessories > Belts")),
    ("Kids", "Accessories", "Hair Accessories"): ("Accessories > Hair accessories",
                                                  ("Accessories > Hairbands & hairclips", "Accessories > Other accessories")),
    ("Kids", "Accessories", "Hats"): ("Accessories > Hats and caps", _k("Accessories > Caps & hats")),
    ("Kids", "Accessories", "Jewelry"): ("Accessories > Jewelry", ("Accessories > Jewelry", "Accessories > Other accessories")),
    ("Kids", "Accessories", "Mittens"): ("Accessories > Gloves", _k("Accessories > Gloves")),
    ("Kids", "Accessories", "Socks & Tights"): ("Accessories > Other", _k("Underwear & socks > Socks")),
    ("Kids", "Accessories", "Ties"): ("Accessories > Other", ("Accessories > Other accessories",
                                                              "Accessories > Ties & bow ties")),
    ("Kids", "Accessories", "Underwear"): (None, _k("Underwear & socks > Underwear")),
}

# One Poshmark subcategory, split by the item's own words (item type, title): (dept, category, subcategory or "*" for
# any, words) -> (Depop "Group > Category" or None to keep, Vinted spec or None to keep). The first that matches wins.
REFINE: list[tuple[str, str, str, str, str | None, Spec]] = [
    ("Women", "Tops", "*", r"\bt-?shirts?\b|\btees?\b", "Tops > T-shirts", None),
    ("Women", "Tops", "Sweatshirts & Hoodies", r"\bhood", "Tops > Hoodies", None),
    ("Women", "Tops", "*", r"\bcorset", "Tops > Corsets", None),
    ("Women", "Sweaters", "Shrugs & Ponchos", r"\bponcho", "Coats and jackets > Jackets",
     "Clothing > Outerwear > Capes & ponchos"),
    ("Women", "Pants & Jumpsuits", "Jumpsuits & Rompers", r"\bromper|\bplaysuit",
     "Jumpsuits and rompers > Rompers", "Clothing > Jumpsuits & rompers > Rompers"),
    ("Women", "Shoes", "Flats & Loafers", r"\bloafer|\bmoccasin|\bdriver", "Footwear > Loafers",
     "Shoes > Boat shoes, loafers & moccasins"),
    ("Women", "Shoes", "Flats & Loafers", r"mary ?janes?", None, "Shoes > Mary Janes & T-strap shoes"),
    ("Women", "Shoes", "*", r"over[- ]the[- ]knee|\bthigh[- ]high", None, "Shoes > Boots > Over-the-knee boots"),
    ("Women", "Shoes", "Heeled Boots", r"\bknee[- ]high|\btall boot", None, "Shoes > Boots > Knee-high boots"),
    ("Women", "Shoes", "Heeled Boots", r"mid[- ]calf", None, "Shoes > Boots > Mid-calf boots"),
    ("Women", "Shoes", "Mules & Clogs", r"\bclog", "Footwear > Clogs", None),
    ("Women", "Shoes", "Sandals", r"\bslides?\b", "Footwear > Slides", "Shoes > Flip-flops & slides"),
    ("Women", "Shoes", "Sandals", r"flip[- ]?flops?", "Footwear > Flip flops", "Shoes > Flip-flops & slides"),
    ("Women", "Shoes", "Winter & Rain Boots", r"\brain", None, "Shoes > Boots > Rain boots"),
    ("Women", "Bags", "Clutches & Wristlets", r"\bwristlet", None, "Bags > Wristlets"),
    ("Women", "Accessories", "Hats", r"\bcap\b|\bbaseball", None, "Accessories > Hats & caps > Caps"),
    ("Women", "Accessories", "Hats", r"\bbeanie", None, "Accessories > Hats & caps > Beanies"),
    ("Women", "Accessories", "Hosiery & Socks", r"\btights?\b|stocking|hosiery|pantyhose", "Underwear > Tights",
     "Clothing > Lingerie & nightwear > Tights & stockings"),
    ("Men", "Shirts", "Sweatshirts & Hoodies", r"\bhood", "Tops > Hoodies", None),
    ("Men", "Shirts", "Sweatshirts & Hoodies", r"\bzip", None,
     "Clothing > Sweaters & sweatshirts > Zip-up hoodies & sweatshirts"),
    ("Men", "Shirts", "*", r"\bplaid|\bcheck|\bflannel|\bgingham|\btartan", None,
     "Clothing > Tops & T-shirts > Shirts > Checked shirts"),
    ("Men", "Shirts", "*", r"\bdenim|\bchambray", None, "Clothing > Tops & T-shirts > Shirts > Denim shirts"),
    ("Men", "Shirts", "Tees - Short Sleeve", r"\bgraphic|\bprint|\blogo", None,
     "Clothing > Tops & T-shirts > T-shirts > Print T-shirts"),
    ("Men", "Shirts", "Tees - Short Sleeve", r"\bstripe", None, "Clothing > Tops & T-shirts > T-shirts > Striped T-shirts"),
    ("Men", "Shirts", "*", r"\bstripe", None, "Clothing > Tops & T-shirts > Shirts > Striped shirts"),
    ("Men", "Shirts", "*", r"\bprint|\bfloral|\bhawaiian|\bpaisley", None, "Clothing > Tops & T-shirts > Shirts > Print shirts"),
    ("Men", "Jeans", "*", r"\bripped|\bdistressed", None, "Clothing > Jeans > Ripped jeans"),
    ("Men", "Jackets & Coats", "Bomber & Varsity", r"\bvarsity|\bletterman", None,
     "Clothing > Outerwear > Jackets > Varsity jackets"),
    ("Men", "Shoes", "Boots", r"\bchelsea|slip[- ]on|pull[- ]on", None, "Shoes > Boots > Chelsea & slip-on boots"),
    ("Men", "Shoes", "Boots", r"\bwork boot|\bsteel", None, "Shoes > Boots > Work boots"),
    ("Men", "Shoes", "*", r"\bsnow", None, "Shoes > Boots > Snow boots"),
    ("Men", "Shoes", "Rain & Snow Boots", r"\brain", None, "Shoes > Boots > Rain boots"),
    ("Men", "Shoes", "Sandals & Flip-Flops", r"flip[- ]?flops?|\bslides?\b", "Footwear > Flip flops",
     "Shoes > Flip-flops & slides"),
    ("Men", "Accessories", "Hats", r"\bcap\b|\bbaseball|\bsnapback|\btrucker", None, "Accessories > Hats & caps > Caps"),
    ("Men", "Accessories", "Hats", r"\bbeanie", None, "Accessories > Hats & caps > Beanies"),
    ("Men", "Accessories", "Jewelry", r"\bnecklace|\bchain", None, "Accessories > Jewelry > Necklaces"),
    ("Men", "Accessories", "Jewelry", r"\bbracelet", None, "Accessories > Jewelry > Bracelets"),
    ("Men", "Accessories", "Jewelry", r"\bring\b", None, "Accessories > Jewelry > Rings"),
    ("Kids", "Shirts & Tops", "*", r"\bt-?shirts?\b|\btees?\b", "Tops > T-shirts", _k("Tops & T-shirts > T-shirts")),
    ("Kids", "Shirts & Tops", "Sweatshirts & Hoodies", r"\bhood", "Tops > Hoodies", None),
    ("Kids", "Bottoms", "Jumpsuits & Rompers", r"\bromper|\bplaysuit", "Jumpsuits and rompers > Rompers", None),
    ("Kids", "Dresses", "*", r"\bmaxi|\blong\b", None, ("Dresses > Long dresses", "Other boys' clothing")),
    ("Kids", "Matching Sets", "*", r"\bshorts?\b", "Bottoms > Shorts", _k("Pants, shorts & overalls > Shorts & cropped pants")),
    ("Kids", "Matching Sets", "*", r"\bskirts?\b|\bskorts?\b", "Bottoms > Skirts", ("Skirts", "Other boys' clothing")),
    ("Kids", "Matching Sets", "*", r"\bleggings?\b", "Bottoms > Leggings", _k("Pants, shorts & overalls > Leggings")),
    ("Kids", "Matching Sets", "*", r"\bpants?\b|\bjoggers?\b|\btrousers?\b", "Bottoms > Pants",
     _k("Pants, shorts & overalls > Other pants, shorts & overalls")),
    ("Kids", "Shoes", "*", r"\bsnow", None, _k("Shoes > Boots > Snow boots")),
    ("Kids", "Shoes", "*", r"\brain ?boots?", None, _k("Shoes > Boots > Rain boots")),
    ("Kids", "Shoes", "Sneakers", r"\bvelcro|hook[- ]and[- ]loop|\bstraps?\b", None,
     _k("Shoes > Sneakers > Hook-and-loop sneakers")),
    ("Kids", "Shoes", "Sneakers", r"slip[- ]on", None, _k("Shoes > Sneakers > Slip-on sneakers")),
    ("Kids", "Shoes", "Sandals & Flip Flops", r"flip[- ]?flops?", "Footwear > Flip flops",
     _k("Shoes > Flip-flops, sandals & slides > Flip-flops")),
    ("Kids", "Shoes", "Sandals & Flip Flops", r"\bslides?\b", "Footwear > Slides",
     _k("Shoes > Flip-flops, sandals & slides > Slides")),
]


class Pick(BaseModel):
    """Where an item goes on one marketplace."""
    value: str                         # Depop: the path; Vinted: the leaf id (as text)
    path: str                          # the full path, both sites
    source: Literal["table", "refined", "learned", "model"]


def gender(kids_gender: str | None) -> str:
    """Vinted's Kids tree is per gender: boys on Boys, everything else (girls, unisex, unread) on Girls — as Poshmark's
    size tab (WO25)."""
    return "Boys" if kids_gender == "boys" else "Girls"


def _vinted_path(dept: str, spec: Spec, kids_gender: str | None) -> str | None:
    if spec is None:
        return None
    if isinstance(spec, tuple):
        sub = spec[0] if gender(kids_gender) == "Girls" else spec[1]
        return f"Kids > {gender(kids_gender)} clothing > {sub}" if sub else None
    return f"{dept} > {spec}"


def _row(dept: str, category: str, sub: str | None) -> tuple[str | None, Spec] | None:
    return TABLE.get((dept, category, sub)) or TABLE.get((dept, category, None))


def from_table(mp: str, dept: str, category: str, sub: str | None, words: str,
               kids_gender: str | None = None) -> Pick | None:
    """The table's answer for one marketplace, refined by the item's words, or None (then the model picks). A target the
    current catalog doesn't have is no answer."""
    row = _row(dept, category, sub)
    if row is None:
        return None
    depop_path, spec = row
    source = "table"
    for r_dept, r_cat, r_sub, rx, r_depop, r_spec in REFINE:
        if (r_dept, r_cat) == (dept, category) and r_sub in ("*", sub) and re.search(rx, words, re.I):
            if mp == "depop" and r_depop:
                depop_path, source = r_depop, "refined"
                break
            if mp == "vinted" and r_spec is not None:
                spec, source = r_spec, "refined"
                break
    if mp == "depop":
        full = f"{dept} > {depop_path}" if depop_path else None
        return Pick(value=full, path=full, source=source) if full and full in catalogs.depop_catalog().categories else None
    full = _vinted_path(dept, spec, kids_gender)
    leaf = catalogs.vinted_catalog().leaf(full) if full else None
    return Pick(value=str(leaf), path=full, source=source) if leaf is not None else None


# ---------------------------------------------------------------- the model, with an enum, cached

def learned_path() -> Path:
    """The learned answers: in the private repo when it is there (seller data, WO30), else next to the catalogs
    (git-ignored there)."""
    from thrift_agent import config
    private = Path(config.PRIVATE_DIR)
    return (private if private.is_dir() else catalogs.DATA_DIR) / "category_map_learned.json"


def _key(mp: str, dept: str, category: str, sub: str | None, kids_gender: str | None, item_type: str) -> str:
    g = gender(kids_gender) if dept == "Kids" and mp == "vinted" else ""
    return "|".join([mp, dept, category, sub or "", g, re.sub(r"\s+", " ", item_type.lower()).strip()])


def _learned() -> dict[str, dict]:
    try:
        return json.loads(learned_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(key: str, pick: Pick) -> None:
    data = _learned()
    data[key] = {"value": pick.value, "path": pick.path}
    file = learned_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    tmp.replace(file)


def candidates(mp: str, dept: str, kids_gender: str | None = None) -> dict[str, str]:
    """{value: path} the model may choose from: the department's Depop paths, or its Vinted leaves (Kids: one gender)."""
    if mp == "depop":
        return {p: p for p in catalogs.depop_catalog().department_paths(dept)}
    leaves = catalogs.vinted_catalog().department_leaves(dept)
    if dept == "Kids":
        prefix = f"Kids > {gender(kids_gender)} clothing > "
        leaves = {cid: p for cid, p in leaves.items() if p.startswith(prefix)}
    return {str(cid): p for cid, p in leaves.items()}


def ask_model(mp: str, dept: str, category: str, sub: str | None, item_type: str, title: str,
              kids_gender: str | None, s=None) -> Pick:
    """Opus picks one of the enumerated options (the tool input is an enum: it can't answer outside the catalog)."""
    from thrift_agent.brain import llm
    from thrift_agent.config import settings

    s = s or settings()
    options = candidates(mp, dept, kids_gender)
    if not options:
        raise CatalogError(f"{mp}: no {dept} categories in the catalog")
    values = tuple(options)
    out = create_model(f"{mp.title()}Category", choice=(Literal[values], Field(  # type: ignore[valid-type]
        description="The one option that fits this item best")))
    listing = "\n".join(f"{v}: {p}" for v, p in options.items()) if mp == "vinted" else "\n".join(values)
    site = "Depop" if mp == "depop" else "Vinted"
    system = (f"You put second-hand clothing listings in {site}'s category tree. Pick the ONE most specific category "
              f"that fits the item. Answer only with one of the listed options.")
    text = (f"Item: {item_type}\nTitle: {title}\nPoshmark category: {dept} > {category}"
            f"{' > ' + sub if sub else ''}{f' (kids: {kids_gender})' if dept == 'Kids' and kids_gender else ''}\n\n"
            f"{site} options{' (id: path)' if mp == 'vinted' else ''}:\n{listing}")
    answer = llm.ask(s["models"].get("crosslist", "claude-opus-5-5"), system, [{"type": "text", "text": text}], out,
                     "pick_category", f"Record the {site} category for this listing")
    choice = str(answer.choice)
    return Pick(value=choice, path=options[choice], source="model")


def resolve(mp: str, dept: str, category: str, sub: str | None, item_type: str, title: str = "",
            kids_gender: str | None = None, ask=None) -> Pick:
    """The table, else a learned answer, else the model (then remembered). `ask` replaces the model (tests)."""
    words = f"{item_type} {title}"
    if (pick := from_table(mp, dept, category, sub, words, kids_gender)) is not None:
        return pick
    key = _key(mp, dept, category, sub, kids_gender, item_type)
    known = _learned().get(key)
    if known and known.get("value") in candidates(mp, dept, kids_gender):
        return Pick(value=known["value"], path=known["path"], source="learned")
    pick = (ask or ask_model)(mp, dept, category, sub, item_type, title, kids_gender)
    if pick.value not in candidates(mp, dept, kids_gender):
        raise CatalogError(f"{mp}: the model's {pick.value!r} is not a {dept} category")
    _remember(key, pick)
    return pick
